"""Content planning and generation — the Celery lane that books a user's calendar, then writes it.

Two beats, deliberately separate. `auto_generate_content` PLANS: per active user it walks the
day-type calendar to the end of the month and inserts empty `planning` rows at fixed UTC instants,
balanced across `PLANNED_POST_TYPES` and classed by the 70/20/10 mix governor.
`auto_create_weekly_content` WRITES: it fills only the `planning` rows due inside that user's
rolling buffer with text, carousels, documents and video, so forward LLM and Runway spend stays
bounded by the buffer rather than by the size of the plan. The `regenerate_*` tasks redo one asset
or one post without disturbing the plan around it.

Cadence is the invariant that bites (issue #621): the plan is NOT one post a day. It fills only
the slots the fixed day-type calendar owns, `posting_days` bounds which weekdays may carry one at
all, and `MIN_POST_INTERVAL` is enforced on the stored UTC instants rather than on dates —
Thu 17:00 and Fri 11:00 are different days and still only 18 hours apart.

Everything from `get_main_blog_url_content` downwards is the source-material half: fetching a
user's own blog or sitemap so a post can be written from something they actually published.
"""

import calendar
import json
import os
import random
from datetime import datetime, timedelta
from typing import Optional, Tuple, Union
from urllib.parse import urlparse
from xml.etree import ElementTree

import pytz
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3 import Retry

from cqc_lem import assets_dir
from cqc_lem.app.my_celery import app as shared_task
from cqc_lem.domain.models import PostDraftContext
from cqc_lem.utilities.ai import story_bank as _story_bank
from cqc_lem.utilities.ai.ai_helper import (
    apply_post_guidance,
    create_runway_video,
    generate_engagement_prompt_post,
    get_ai_linked_post_refinement,
    get_blog_summary_post_from_ai,
    get_flux_image_prompt_from_ai,
    get_industry_news_post_from_ai,
    get_or_create_profile_synthesis,
    get_personal_story_post_from_ai,
    get_runway_ml_video_prompt_from_ai,
    get_thought_leadership_post_from_ai,
    get_website_content_post_from_ai,
    optimize_post_hook,
)
from cqc_lem.utilities.ai.content_alignment import (
    ContentMix,
    assign_content_mix,
    authenticity_gate_enabled,
    authenticity_score_min,
    contains_meeting_ask,
    ensure_lead_magnet_cta,
    humanize_text,
    lead_magnet_cta_directive,
    meeting_ask_excerpts,
    normalize_content_mix,
    personal_proof_directive,
    profile_topic_dna,
    replace_meeting_ask_cta,
    score_authenticity,
    should_include_lead_magnet_cta,
    topic_authority_min,
    topic_authority_score,
)
from cqc_lem.utilities.ai.content_framework import (
    day_type_for_weekday,
    day_type_formats,
    day_type_stage,
    deck_slides,
    dwell_report,
    dwell_score_min,
    fact_anchored_formats,
    fact_grounding_report,
    fact_retry_directive,
    has_first_person_proof,
    history_avoidance_directive,
    occasion_stage,
    post_similarity_report,
    requires_fact_anchor,
    select_blueprint,
    shape_for_dwell,
    weekly_post_slots,
)
from cqc_lem.utilities.ai.slop_lint import lint_report as slop_lint_report, slop_retry_directive, violation_reasons
from cqc_lem.utilities.content_generation_status import (
    ContentGenerationEmptyReason,
    mark_empty,
    mark_finished,
    mark_in_progress,
    record_post_failed,
    record_post_generated,
)
from cqc_lem.utilities.db import (
    DEFAULT_CONTENT_BUFFER_DAYS,
    DEFAULT_CONTENT_BUFFER_MAX_POSTS,
    DEFAULT_POSTING_DAYS,
    DEFAULT_POSTS_PER_WEEK,
    MAX_CONTENT_BUFFER_DAYS,
    MAX_CONTENT_BUFFER_POSTS,
    POSTS_PER_WEEK_MAX,
    POSTS_PER_WEEK_MIN,
    PostStatus,
    PostType,
    count_ready_posts_within_buffer,
    get_active_user_ids,
    get_engagement_preferences,
    get_last_planned_post_date_for_user,
    get_lead_magnet_settings,
    get_newsletter_settings,
    get_next_planned_post_date,
    get_next_planned_posts_after_buffer,
    get_planned_posts_within_buffer,
    get_post_authenticity_score,
    get_post_content,
    get_post_content_mix,
    get_post_gate_reason,
    get_post_type_counts,
    get_recent_post_shape_history,
    get_recent_post_texts,
    get_shape_performance,
    get_story_bank_entries,
    get_user_blog_url,
    get_user_ids_with_planned_posts_within_buffer,
    get_user_password_pair_by_id,
    get_user_preferences,
    get_user_sitemap_url,
    get_user_timezone,
    insert_planned_post,
    normalize_posting_days,
    record_story_bank_use,
    update_db_post_authenticity_score,
    update_db_post_carousel_slides,
    update_db_post_content,
    update_db_post_dwell_score,
    update_db_post_gate_reason,
    update_db_post_shape,
    update_db_post_status,
    update_db_post_video_url,
)
from cqc_lem.utilities.env_constants import (
    AI_DISCLOSURE_ENABLED,
    AI_DISCLOSURE_TEXT,
    API_URL_FINAL,
    AVATAR_LIKENESS_PROBE_ENABLED,
    AVATAR_LIKENESS_VIDEO_HOLD_ENABLED,
    DEFAULT_IMAGE_RATIO,
    DEFAULT_VIDEO_RATIO,
    PREMIUM_TOP_VIDEO_CREDITS,
    PREMIUM_TOP_VIDEO_MODEL,
    PREMIUM_VIDEO_CREDITS,
    PREMIUM_VIDEO_MODEL,
    STANDARD_VIDEO_MODEL,
    VIDEO_PROBE_ENABLED,
    VIDEO_PROBE_MIN_SIZE_BYTES,
)
from cqc_lem.utilities.linkedin.helper import get_my_profile, load_profile_for_user
from cqc_lem.utilities.linkedin.profile import LinkedInProfile
from cqc_lem.utilities.linkedin.rate_limit import acquire_run_lock, release_run_lock
from cqc_lem.utilities.linkedin_formatter import sanitize_for_linkedin, strip_engagement_bait
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning
from cqc_lem.utilities.notifications import notify_content_generation_ready
from cqc_lem.utilities.observability import (
    FEATURE_CONTENT,
    attribute_llm_cost,
    llm_attribution,
    llm_pipeline,
    llm_step,
    track_video_asset_probe,
)
from cqc_lem.utilities.quality_gates import (
    GATE_MALFORMED_ASSET,
    GATE_SIMILARITY,
    GATE_SLIDE_SLOP,
    affiliate_promo_finding,
    authenticity_finding,
    demoting_findings,
    fact_grounding_finding,
    focus_finding,
    malformed_asset_finding,
    meeting_cta_finding,
    missing_asset_finding,
    similarity_finding,
    slide_slop_finding,
    slop_finding,
)
from cqc_lem.utilities.selenium_util import get_driver_wait_pair, quit_gracefully
from cqc_lem.utilities.utils import (
    apply_schedule_jitter,
    create_folder_if_not_exists,
    get_post_time,
    save_video_url_to_dir,
)

# Post types the 30-day plan balances across. Native documents (PDF decks) are 2026's
# highest-reach format, so they get an equal share alongside text/carousel/video.
PLANNED_POST_TYPES = [PostType.CAROUSEL.value, PostType.TEXT.value,
                      PostType.VIDEO.value, PostType.DOCUMENT.value]

# Never more than one post in any 24-hour window (issue #621 / G6). The day-type calendar can put
# slots on back-to-back weekdays, and the peak model's hours vary by weekday (Thu 17:00 → Fri 11:00
# is only 18h), so the gap is enforced on the stored UTC instants rather than assumed from dates.
MIN_POST_INTERVAL = timedelta(hours=24)


@shared_task.task
def auto_generate_content():
    """Beat entry point for planning: queue one `plan_content_for_user` task per active user.

    Does no planning itself. Each user's month is planned as its own task, so a user whose plan
    fails or runs long neither blocks nor half-plans anybody else's.
    """
    # Get active users from DB
    active_users = get_active_user_ids()
    # For each user generate content for the next 30 days
    for user_id in active_users:
        plan_content_for_user.apply_async(kwargs={"user_id": user_id})


def _plan_window_end(start_date: datetime) -> datetime:
    """The last day this planning run may schedule into — the end of START DATE's month.

    Anchored to the start date's own month, not "this" month: a start date that has already rolled
    into the next month must not be handed the previous month's day count (Jan 31 → Feb 1 has no
    31st, which would raise).
    """
    return start_date.replace(day=calendar.monthrange(start_date.year, start_date.month)[1])


def _cadence_slots(user_id: int, start_date: datetime, end_date: datetime) -> list:
    """The dates this user's day-type calendar fills between two dates, inclusive.

    Cadence, not volume, is the 2026 lever (issue #621 / G6): instead of one post on every remaining
    day of the month, a user publishes on the `posts_per_week` weekdays their fixed day-type
    calendar owns. `posting_days` (issue #581) then says WHICH weekdays are eligible at all —
    Mon-Fri by default, so weekends are opt-in rather than what raising the cadence to 6-7/week
    happens to produce. Falls back to the shipped defaults when preferences can't be read — never
    to daily, and never to an empty week.
    """
    posting_days = list(DEFAULT_POSTING_DAYS)
    try:
        prefs = get_engagement_preferences(user_id) or {}
        raw = prefs.get("posts_per_week")
        posts_per_week = DEFAULT_POSTS_PER_WEEK if raw is None else int(raw)
        if prefs.get("posting_days") is not None:
            posting_days = normalize_posting_days(prefs.get("posting_days"))
    except Exception as e:
        log_warning(f"Could not read posting cadence for user {user_id} — using the default",
                    exc=e, user_id=user_id, task_name="plan_content_for_user")
        posts_per_week = DEFAULT_POSTS_PER_WEEK
    posts_per_week = max(POSTS_PER_WEEK_MIN, min(POSTS_PER_WEEK_MAX, posts_per_week))
    weekdays = set(weekly_post_slots(posts_per_week, allowed_days=posting_days))
    if len(weekdays) < posts_per_week:
        # The allow-list is the harder bound — say so, because the user asked for more posts than
        # the days they left switched on can carry.
        log_info(f"Posting cadence capped by the configured days for user {user_id}: "
                 f"{posts_per_week}/week requested, {len(weekdays)} eligible day(s)",
                 user_id=user_id, task_name="plan_content_for_user")

    slots = []
    day = start_date.date()
    last = end_date.date()
    while day <= last:
        if day.weekday() in weekdays:
            slots.append(day)
        day += timedelta(days=1)
    return slots


def _schedule_slot_utc(post_date, user_id: int, previous_utc: Optional[datetime]) -> datetime:
    """One slot's stored (naive UTC) time: the golden/peak hour for that weekday, clamped into the
    user's waking hours, jittered 15-30 minutes, converted from the user's zone to UTC, and finally
    held at least 24h after the previous post so a plan can never put two posts in one day-window
    (docs/timezone-contract.md, issue #621).
    """
    post_time = apply_schedule_jitter(get_post_time(post_date, user_id))
    scheduled_datetime = datetime.combine(post_date, post_time)
    try:
        user_tz = pytz.timezone(get_user_timezone(user_id))
        scheduled_datetime = (
            user_tz.localize(scheduled_datetime).astimezone(pytz.utc).replace(tzinfo=None)
        )
    except Exception as e:
        log_warning(f"Timezone conversion failed for user {user_id} — storing as UTC",
                    exc=e, user_id=user_id, task_name="plan_content_for_user")
    if previous_utc is not None and scheduled_datetime - previous_utc < MIN_POST_INTERVAL:
        scheduled_datetime = previous_utc + MIN_POST_INTERVAL + timedelta(minutes=1)
    return scheduled_datetime


@shared_task.task(bind=True, reject_on_worker_lost=True, rate_limit='1/m')
def plan_content_for_user(self, user_id: int):
    """Generate and plan content through the end of the month on the user's day-type calendar.
    Ensures a balanced distribution of post types (carousel, text, video, document); the buyer
    journey stage now comes from the weekly slot's job rather than a uniform round-robin.
    """
    # 1. Review existing content in the database
    # Query the database to count the current representation of each post_type in the 'posts' table
    # Example: SELECT COUNT(*) FROM posts WHERE post_type = 'carousel'
    current_counts = get_post_type_counts(user_id)

    # Calculate the total posts and percentages of each type
    total_posts = sum(current_counts.values())
    # The 70/20/10 governor's rotation offset: continuing from how many posts this user already has
    # stops a new plan from restarting the promo cadence (which could place two promo posts back to
    # back across the plan boundary).
    existing_post_count = total_posts
    if total_posts == 0:
        # New user with no posts — treat all types as equally unrepresented
        percentages = {post_type: 0.0 for post_type in current_counts}
    else:
        percentages = {post_type: (count / total_posts) * 100 for post_type, count in current_counts.items()}

    # Log the current representation for debugging
    # myprint(f"Current Content Percentages: {percentages}")

    # 2. Determine the target distribution of post types for the next 30 days
    # Based on the percentages, calculate how many posts of each type are needed to balance the content

    # Determine how many days are in this month
    now = datetime.now()
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    log_info(f"Days in Month: {days_in_month}")

    # Determine the start date as the day after the last scheduled post in planning status
    last_planned_date = get_last_planned_post_date_for_user(user_id)

    # myprint(f"Last Planned Date: {last_planned_date}")

    # Use the last planned date if it exists and it is after today
    if last_planned_date and last_planned_date.date() > datetime.now().date():
        start_date = last_planned_date + timedelta(days=1)  # Start with the next day

        # If the start date is greate than 30 days from today just skip the process
        if start_date > datetime.now() + timedelta(days=30):
            log_info(f"Content Plan | Start Date: {start_date} | >30 days out | Skipped")
            return
    else:
        start_date = datetime.now() + timedelta(days=1)  # Start with the next day
    log_info(f"Content Plan | Start Date: {start_date}")



    # The plan still runs to the end of the month, but the SLOTS in that window come from the
    # user's day-type calendar (issue #621) instead of one post on every remaining day.
    end_of_month = _plan_window_end(start_date)
    slots = _cadence_slots(user_id, start_date, end_of_month)
    if not slots:
        log_info(f"Content Plan | No cadence slots left this month after {start_date} | Skipped")
        return

    target_posts = len(slots)
    log_info(f"Content Plan | {target_posts} slot(s) through {end_of_month.date()} "
            f"on weekdays {sorted({s.weekday() for s in slots})}")

    needed_posts = {post_type: target_posts // len(PLANNED_POST_TYPES) for post_type in PLANNED_POST_TYPES}

    # myprint(f"Needed Posts: {needed_posts}")

    # Ensure all post types are present in current_counts and percentages
    for post_type in PLANNED_POST_TYPES:
        if post_type not in percentages:
            percentages[post_type] = 0.0

    # myprint(f"Updated Content Percentages: {percentages}" )

    # Loop until the total needed posts equals the target posts
    while sum(needed_posts.values()) != target_posts:
        max_key = get_max_key(percentages)
        min_key = get_min_key(percentages)

        # Adjust the counts
        if sum(needed_posts.values()) < target_posts:
            needed_posts[min_key] += 1
        else:
            needed_posts[max_key] -= 1

        # Recalculate percentages
        total_posts = sum(needed_posts.values())
        for post_type in percentages:
            percentages[post_type] = (needed_posts[post_type] / total_posts) * 100

    # Output the final needed_posts and percentages
    # myprint(f"Final Needed Posts: {needed_posts}")
    # myprint(f"Final Percentages: {percentages}")

    # 3. Fill each calendar slot. The buyer-journey stage now comes from the slot's day type — a
    # Tuesday build-receipt is a decision-stage post whatever else the plan holds — which replaces
    # the old uniform awareness/consideration/decision round-robin.
    daily_plan = []

    # Create a list of post types based on needed_posts
    post_types = []
    for post_type, count in needed_posts.items():
        post_types.extend([post_type] * count)

    # Shuffle the post types to ensure a random order
    random.shuffle(post_types)

    # 70/20/10 content-mix governor (issue #618): classify every planned post BEFORE post types are
    # handed out, so the one-in-ten promo slot can claim a text post (its case-study/no-pressure
    # shape is steered in the text-post prompt) and the rest stay audience-value/authority content.
    mix_classes = assign_content_mix(target_posts, offset=existing_post_count)

    # The 24h floor also has to hold ACROSS planning runs. `slots` only knows about this run, so a
    # plan that starts the day after the previous plan's last post — a 7/week calendar rolling into
    # a new month, or a user raising their cadence mid-month — would otherwise stack two posts
    # inside one 24h window. Seed the floor with the user's last already-scheduled post while it is
    # still ahead of us; get_last_planned_post_date_for_user returns the stored naive-UTC instant,
    # which is exactly the clock the floor is measured on.
    previous_utc = None
    try:
        if last_planned_date is not None and last_planned_date > datetime.now():
            previous_utc = last_planned_date
    except TypeError:  # a date (not a datetime) row — fall back to spacing within this run only
        previous_utc = None
    for day, post_date in enumerate(slots):
        content_mix = mix_classes[day]

        # Choose a post type from the shuffled list
        post_type = _take_planned_post_type(post_types, content_mix)

        stage = day_type_stage(post_date.weekday())

        scheduled_datetime = _schedule_slot_utc(post_date, user_id, previous_utc)
        previous_utc = scheduled_datetime

        daily_plan.append({
            "scheduled_datetime": scheduled_datetime,
            "post_type": post_type,
            "stage": stage,
            "content_mix": content_mix
        })

        # Update the needed_posts count for the selected post type
        needed_posts[post_type] -= 1

    # Log the final content plan for the next 30 days
    # myprint(f"Generated content plan: {daily_plan}")
    log_info("Generated content plan")

    # 4. Save the daily plan to the database for tracking and scheduling
    save_content_plan(user_id, daily_plan)


def _take_planned_post_type(post_types: list, content_mix: str = None) -> str:
    """Pop the next post type off the shuffled plan list. The promo slot PREFERS a text post because
    the governor requires a case-study-shaped, no-pressure BODY, and that shaping is steered in the
    text-post prompt (carousels/videos run their own generators). Falls back to whatever is left, so
    a plan of only carousels still schedules its promo slot.
    """
    if content_mix == ContentMix.PROMO.value and PostType.TEXT.value in post_types:
        post_types.remove(PostType.TEXT.value)
        return PostType.TEXT.value
    return post_types.pop()


# Function to find the key with the highest value in a dictionary
def get_max_key(d):
    """Key with the highest value — the over-represented post type a slot is taken away from.

    Used by `plan_content_for_user`'s distribution loop. Ties go to whichever key `max` reaches
    first, which is fine here: the loop runs until the totals match, so an arbitrary tie-break
    only decides WHICH equally-represented type moves.
    """
    return max(d, key=d.get)


# Function to find the key with the lowest value in a dictionary
def get_min_key(d):
    """Key with the lowest value — the under-represented post type a slot is given to.

    The other half of `plan_content_for_user`'s distribution loop; see `get_max_key`.
    """
    return min(d, key=d.get)


def create_content(user_id: int, post_type: str, stage: str, post_id: int = None,
                   content_mix: str = None, day_weekday: int = None):
    """Create content based on the specified post type and buyer journey stage.

    `content_mix` is the post's 70/20/10 class from the plan governor (issue #618) — it steers the
    text-post prompt.

    `day_weekday` is the slot's LOCAL weekday (Mon=0 … Sun=6); it selects the day-type calendar's
    archetype family for text posts (issue #621) so a Wednesday reads as a story and a Thursday as
    a spiky POV. Carousels and videos carry their own template menus and are unaffected.
    """
    video_url = None
    content = None

    if post_type == PostType.VIDEO.value:
        content, video_url = create_video_content(user_id, stage, post_id=post_id)
    elif post_type in (PostType.CAROUSEL.value, PostType.DOCUMENT.value):
        # A document post IS a carousel deck — same generated slides, published as a
        # native PDF instead of a multi-image share (see share_document_on_linkedin).
        content = create_carousel_content(user_id, stage, post_id)
    else:
        # Affiliate promotion (issue #770) CLAIMS this promo slot rather than adding a post beside
        # it, which is what keeps LEM promotion inside the same 10% ceiling the author's own case
        # studies live under. It writes only for a user who recorded (B) consent, on 1 in N promo
        # slots; every other slot falls through to the ordinary case-study post unchanged.
        content = _affiliate_promo_content(user_id, post_id, content_mix)
        if content is None:
            content = create_text_post(user_id, stage, post_id=post_id, content_mix=content_mix,
                                       day_weekday=day_weekday)
        if content:
            content = content.strip()
            # Single-image post: LinkedIn's highest-volume format. Best-effort — a text post
            # publishes fine bare, so an image failure never costs the post.
            _generate_text_post_image(user_id, content, post_id)

    return content, video_url


def _generate_text_post_image(user_id: int, text_content: str, post_id: "int | None") -> "str | None":
    """Generate + store the AI image a generated TEXT post publishes with.

    Gated on the user's `text_post_images` preference — that gate is what this wrapper adds; the
    render itself is `post_image.generate_image_for_post`, the ONE engine the Content Studio's
    manual "Generate image" button also calls (issue #1030).
    Never raises: `posts.image_url` simply stays NULL and the post ships bare.
    """
    if not post_id or not text_content:
        return None
    try:
        from cqc_lem.utilities.db import get_engagement_preferences, update_db_post_image_url
        from cqc_lem.utilities.post_image import generate_image_for_post

        if not get_engagement_preferences(user_id).get("text_post_images", True):
            return None

        api_image_url, _reason = generate_image_for_post(user_id, text_content, post_id=post_id)
        if not api_image_url:
            return None
        update_db_post_image_url(post_id, api_image_url)
        log_info(f"_generate_text_post_image: post_id={post_id} -> {api_image_url}")
        return api_image_url
    except Exception as e:
        log_warning("Could not generate the post image — the post ships without one", exc=e,
                    user_id=user_id, post_id=post_id, task_name="create_content")
        return None


def _affiliate_promo_content(user_id: int, post_id: Optional[int],
                             content_mix: Optional[str]) -> Optional[str]:
    """The LEM-promotional post for this slot, or None to write the author's own post instead.

    Never raises: the (B) writer is an opt-in extra, and a user who consented to it must not lose
    the post they would otherwise have had because it failed.
    """
    try:
        from cqc_lem.utilities.marketing.affiliate_content import claims_promo_slot, generate_promo_post
        # Asked BEFORE the prefs/synthesis reads: the answer is no on almost every post, and a
        # voice-synthesis read per planned post is a real cost to pay for an answer of "not this one".
        if not claims_promo_slot(user_id, post_id, content_mix):
            return None
        return generate_promo_post(user_id, post_id=post_id, content_mix=content_mix,
                                   prefs=_engagement_prefs_or_empty(user_id),
                                   profile_synthesis=_profile_synthesis_or_none(user_id))
    except Exception as e:
        log_warning("Affiliate promo generation failed — writing the ordinary promo post instead",
                    exc=e, user_id=user_id, post_id=post_id, task_name="create_content")
        return None


def _profile_synthesis_or_none(user_id: int) -> Optional[str]:
    """The user's cached voice synthesis with no Selenium fallback — promotional copy is worth
    matching the author's voice, never worth a browser session.
    """
    try:
        return get_or_create_profile_synthesis(user_id, load_profile_for_user(user_id))
    except Exception as e:
        log_warning("Could not load the profile synthesis — writing without the user's voice", exc=e,
                    user_id=user_id, task_name="create_content")
        return None


def _fact_anchors(user_id: int) -> list:
    """The VERIFIED, human-sourced facts a fact-anchored archetype's specifics are CHECKED against
    (issue #619 / G4) — the numbers a draft is allowed to have written down. The story bank
    (issue #620 / G5) is that source, and this is every active entry, not just the one entry a given
    post was anchored to: a number out of the user's own material is by definition not something the
    model invented, and falsely calling a real figure fabricated is the one error the guard must not
    make. What the WRITER may state is narrower — see the anchors handed to `blueprint_directive`.

    An empty or unreadable bank returns [], which runs everything downstream in its strictest mode:
    every specific must ship as a marked placeholder, and a draft that stated one anyway is held for
    review. Never raises — a bank read that fails costs the credit, never the post.
    """
    try:
        entries = get_story_bank_entries(user_id, active_only=True)
    except Exception as e:
        log_warning("Story bank unreadable — running the fact gates with no verified anchors",
                    exc=e, user_id=user_id, task_name="create_content")
        return []
    return [source for entry in (entries or []) for source in _story_bank.fact_sources(entry)]


def _fact_anchors_for(user_id: int, archetype: Optional[str]) -> list:
    """The verified facts for the gate pass on THIS post — read only when the post's archetype is
    actually fact-anchored, so an ordinary post never pays for a story-bank read it cannot use.
    """
    return _fact_anchors(user_id) if requires_fact_anchor("post", archetype) else []


def _select_post_blueprint(user_id: int, prefer_save_targeted: bool = False,
                           guidance: Optional[str] = None,
                           exclude_formats: Optional[list] = None,
                           preferred_formats: Optional[list] = None) -> dict:
    """ONE assigned SHAPE from the shared framework core: rotate away from this user's recently used
    archetypes/hook styles (V51 history) and bias away from the ones that historically
    under-perform (#389). Chosen in code — no extra LLM call. `guidance` pins the archetype when the
    slot dictates it (the 70/20/10 promo slot needs a case study — #618); `prefer_save_targeted` only
    biases toward one, `preferred_formats` narrows the menu to the day-type calendar's family
    (#621), and `exclude_formats` takes archetypes off the menu for a caller that cannot
    honor their contract. Every input is best-effort: a history or performance read that fails costs
    the steering, never the post.
    """
    try:
        shape_history = get_recent_post_shape_history(user_id)
    except Exception as e:
        log_warning("Could not load the post shape history — rotating without it", exc=e,
                    user_id=user_id, task_name="create_content")
        shape_history = []
    try:
        performance = get_shape_performance(user_id)
    except Exception as e:
        log_warning("Could not load shape performance — selecting without it", exc=e,
                    user_id=user_id, task_name="create_content")
        performance = None
    return select_blueprint(
        "post",
        recent_formats=[h.get("archetype") for h in shape_history if h.get("archetype")],
        recent_hook_styles=[h.get("hook_style") for h in shape_history if h.get("hook_style")],
        performance=performance,
        prefer_save_targeted=prefer_save_targeted,
        exclude_formats=exclude_formats,
        preferred_formats=preferred_formats,
        guidance=guidance)


def _select_carousel_blueprint(user_id: int, fact_anchors: Optional[list] = None) -> Optional[dict]:
    """The carousel's shape — the same post menu, biased toward the save-targeted archetypes since a
    document post is where a saved reference actually lives. Both halves of that hang on having
    verified facts: with none, the fact-anchored archetypes are taken OFF the carousel menu entirely
    (not merely un-preferred), because their no-fabrication contract ships every specific as a
    `[[…]]` placeholder — and a carousel bakes its text into rendered slide IMAGES, which no edit or
    re-score can ever fix. The caller passes the WRITER's anchors (the one selected story's facts,
    issue #728) — that, not the size of the whole bank, is what this deck can actually write from;
    the None fallback reads the bank only because a caller with no story selected has nothing
    narrower to offer. Never raises — a carousel that loses its archetype still generates from the
    generic slide guidance.
    """
    try:
        anchors = _fact_anchors(user_id) if fact_anchors is None else fact_anchors
        return _select_post_blueprint(
            user_id, prefer_save_targeted=bool(anchors),
            exclude_formats=None if anchors else fact_anchored_formats("post"))
    except Exception as e:
        log_warning("Could not select a carousel archetype — using generic slide guidance", exc=e,
                    user_id=user_id, task_name="create_carousel_content")
        return None


def _report_carousel_fact_grounding(user_id: int, post_id: Optional[int], blueprint: Optional[dict],
                                    carousel: Optional[dict]) -> None:
    """Grade a generated DECK's specifics against the user's whole verified bank (issue #728) and log
    anything it could not back. Only for the fact-anchored archetypes, whose value IS the specifics.
    Advisory by design: the slides are rendered into images, so there is nothing a hold could fix —
    the point is that an invented slide number is visible instead of silent. Never raises.
    """
    fmt = (blueprint or {}).get("format")
    if not carousel or not requires_fact_anchor("post", fmt):
        return
    try:
        report = fact_grounding_report(_deck_text(carousel), _fact_anchors(user_id))
        if report["unverified_values"]:
            log_warning("Carousel slides state specifics no verified fact backs: "
                        + ", ".join(report["unverified_values"][:5]),
                        user_id=user_id, post_id=post_id, task_name="create_carousel_content")
        if report["placeholders"]:
            log_warning("Carousel slides carry unfilled placeholders that will be rendered into the "
                        "images: " + ", ".join(report["placeholders"][:5]),
                        user_id=user_id, post_id=post_id, task_name="create_carousel_content")
    except Exception as e:
        log_warning("Could not grade the carousel's fact grounding", exc=e, user_id=user_id,
                    post_id=post_id, task_name="create_carousel_content")


def _deck_text(carousel: Optional[dict]) -> str:
    """Every slide's title + body, in schema order, as ONE block of text.

    This is what a slide-level TEXT check grades. The cover and CTA slides are included on purpose:
    `deck_slides`' `graded` flag exempts them from the REFERENCE gate, whose question is "does this
    slide carry a reusable artifact" — one a cover cannot answer — while a bait closer or a canned
    opener is exactly what a text-quality check exists to catch, and those live on the very slides
    the reference gate skips.
    """
    return "\n".join(f"{s['title']} {s['content']}".strip()
                     for s in deck_slides(carousel)).strip()


def _record_slide_slop_finding(user_id: int, post_id: int, report: dict) -> None:
    """Record — or clear — the slide-level AI-slop note on `posts.gate_reason` (issue #1512).

    Same mechanic as `_record_video_probe_finding`, and for the same reason: the slide TEXT exists
    only inside `create_carousel_content` (the deck is persisted as rendered image URLs), so a gate
    pass that runs later cannot re-derive this verdict — it has to be written where it is measured
    and re-read where the findings are assembled. A clean deck clears any note an earlier generation
    left, so a regenerated carousel stops showing a stale one.

    ADVISORY (`demoted=False`): a hold on baked-in slide text can only be cleared by regenerating
    the whole deck, which is a product decision that is not this change's to make.

    Best-effort — this is review UX, so an unreadable/unwritable reason only logs.
    """
    try:
        existing = get_post_gate_reason(post_id)
        kept = [f for f in existing if f.get("gate") != GATE_SLIDE_SLOP]
        if not report["violations"]:
            if len(kept) == len(existing):
                # Nothing to clear — a clean deck never writes.
                return
            findings = kept
        else:
            findings = kept + [slide_slop_finding(violation_reasons(report["hard"]),
                                                  violation_reasons(report["warnings"]))]
        update_db_post_gate_reason(post_id, findings)
    except Exception as e:
        log_warning("Could not record the carousel's slide-level slop lint — this deck's review "
                    "queue entry will not name the patterns its slides carry", exc=e,
                    user_id=user_id, post_id=post_id, task_name="create_carousel_content")


def _report_carousel_slide_slop(user_id: int, post_id: Optional[int],
                                carousel: Optional[dict]) -> None:
    """Grade a deck's SLIDE text with the EXISTING slop lint and record what fired (issue #1512).

    The caption has always run the full gate suite; the slides ran one gate (`deck_reference_report`)
    and an advisory fact-grounding log, so on the one format where the slides ARE the post a tier-1
    tell pileup or a banned scaffold opener was recorded nowhere. This runs `lint_report` — the same
    linter, the same `post` surface severities, never a carousel-only copy — over the concatenated
    slide text and persists the result as an advisory finding.

    Records only; it never changes a post's status. Never raises: a slide reading that fails costs
    the record, never the deck.
    """
    if post_id is None or not carousel:
        return
    try:
        deck_text = _deck_text(carousel)
        if not deck_text:
            return
        # The same lead-magnet exemption the caption's lint gets: a sanctioned "Comment KEYWORD" CTA
        # is not bait, wherever in the piece it is written.
        report = slop_lint_report(deck_text, "post", exempt_keyword=_cta_keyword_for(user_id, post_id))
        if not report["checked"]:
            return
        _record_slide_slop_finding(user_id, post_id, report)
        if report["violations"]:
            # INFO, not WARNING: this is a measurement of a draft, and a deck carrying a warn-level
            # pattern is an expected reading rather than a degraded path — warning here would file a
            # grouped defect against working behaviour on every slop-carrying deck.
            log_info("Carousel slide text matched the AI-slop lint: "
                     + ", ".join(v["check"] for v in report["violations"]),
                     user_id=user_id, post_id=post_id, task_name="create_carousel_content")
    except Exception as e:
        log_warning("Could not lint the carousel's slide text", exc=e, user_id=user_id,
                    post_id=post_id, task_name="create_carousel_content")


def _score_carousel_caption_authenticity(user_id: int, post_id: Optional[int], caption: str,
                                         profile_synthesis: Optional[str] = None,
                                         prefs: Optional[dict] = None) -> None:
    """Judge a deck's CAPTION with the same authenticity judge a text post's draft gets (issue #1512).

    `_score_and_persist_authenticity` was only ever called from `create_text_post` and
    `rescore_post`, so `posts.authenticity_score` stayed NULL for every generated deck and the
    authenticity gate inside `evaluate_post_gates` skipped itself by its own `is not None` guard —
    every carousel caption shipped unjudged unless a human pressed re-score. A caption is a post
    like any other, so it is judged like one: ONE `lem-medium` judge call per deck, and the gate that
    reads the score back demotes a low-scoring deck to PENDING exactly as it does a text post.

    The SLIDES are not judged here — their reading is the deterministic slop lint above; the judge
    grades the caption only, which is what the review queue can actually be edited and re-scored on.
    """
    if post_id is None or not (caption or "").strip():
        return
    _score_and_persist_authenticity(user_id, post_id, caption, load_profile_for_user(user_id),
                                    profile_synthesis, prefs,
                                    task_name="create_carousel_content")


@attribute_llm_cost(FEATURE_CONTENT)
def create_carousel_content(user_id: int, stage: str, post_id: int = None,
                            template: Optional[str] = None,
                            guidance: Optional[str] = None) -> str:
    """Generate AI carousel content, render slide images, update DB, and return the post text.

    `guidance` is the user's free-text revision request from the regenerate flow (issue #794) — it
    steers the CAPTION AND the slides, since on a deck the slides are the post.
    """
    from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
    from cqc_lem.utilities.carousel_creator import (
        CaseStudyCarousel,
        EducationalContentCarousel,
        IndustryInsightsCarousel,
        ProductDemoCarousel,
        create_carousel_slide_images,
        create_ppt,
    )

    # Same alignment inputs as text posts (best-effort — carousel generation never blocks on them).
    try:
        prefs = get_engagement_preferences(user_id)
    except Exception as e:
        log_warning("Could not load engagement preferences for the carousel", exc=e,
                    user_id=user_id, post_id=post_id, task_name="create_carousel_content")
        prefs = None
    try:
        profile_synthesis = get_or_create_profile_synthesis(user_id)
    except Exception as e:
        log_warning("Could not load the profile synthesis for the carousel", exc=e,
                    user_id=user_id, post_id=post_id, task_name="create_carousel_content")
        profile_synthesis = None

    # ONE story-bank anchor per piece (issue #728), the same split text posts have always had: the
    # WRITER gets the single selected entry, the CHECKERS keep the whole bank. Handing the writer
    # every active entry is what produced a six-receipt greatest-hits deck that spent the account's
    # best material in one post. Selected BEFORE the blueprint, because whether we HAVE an anchor is
    # what decides if a fact-anchored archetype is even on the carousel menu.
    story = _select_story_for_post(user_id, prefs)
    story_directive = _story_bank.story_directive(story)
    writer_anchors = _story_bank.fact_sources(story) if story else []
    if story:
        log_info(f"Story bank anchor for carousel post_id={post_id}: "
                f"[{story.get('kind')}] {story.get('title')}")

    # Carousels draw their SHAPE from the same shared post menu as text posts (issue #619 / G4) and
    # rotate against the same V51 shape history, so a build receipt can land as a document post —
    # and so a carousel archetype counts against the next text post's rotation.
    blueprint = _select_carousel_blueprint(user_id, writer_anchors)
    post_text, carousel_dict = generate_carousel_content(user_id, stage, prefs=prefs,
                                                         profile_synthesis=profile_synthesis,
                                                         blueprint=blueprint,
                                                         fact_anchors=writer_anchors,
                                                         story_directive=story_directive,
                                                         guidance=guidance)
    log_info(f"Carousel AI content generated for user_id={user_id} stage={stage} "
            f"archetype={(blueprint or {}).get('format')}")

    # The verification half of the split: the SLIDES are checked against EVERY active bank entry,
    # because a number out of the user's own material is by definition not one the model invented.
    # Reported, never held — the caption already has its gate (`evaluate_post_gates` runs the same
    # check with the same full bank), and slide text has no review queue to be held in.
    _report_carousel_fact_grounding(user_id, post_id, blueprint, carousel_dict)

    # The slide text's own quality reading (issue #1512): the same slop lint the caption runs, over
    # the concatenated slides. Recorded on the post, advisory — it holds nothing, and it leaves
    # `_report_carousel_fact_grounding`'s advisory-only posture (ring-fenced by #1139) untouched.
    _report_carousel_slide_slop(user_id, post_id, carousel_dict)

    # The caption's authenticity judge (issue #1512, owner decision 2A on PR #1554): a carousel
    # caption is a post like any other, and until now it was the one generated post type that
    # reached the gate pass with a NULL score, so the authenticity gate skipped every deck. Scored
    # here rather than after the render because this is the finished caption — the render can only
    # fail the post, never rewrite its text.
    _score_carousel_caption_authenticity(user_id, post_id, post_text, profile_synthesis, prefs)

    # Count the anchor as used so the NEXT piece rotates to different raw material — the same write
    # create_text_post makes. Without it the carousel reads the bank but never advances it, so
    # `select_story`'s least-used ordering hands every deck the SAME entry and the next text post
    # re-uses it too: one anchor per post, but the same anchor every post. Only when a deck really
    # came out of it — a failed generation must not burn the anecdote.
    if story and story.get("id") and (post_text or carousel_dict):
        try:
            record_story_bank_use(user_id, int(story["id"]))
        except Exception as e:
            # WARNING, not INFO: the write is what ADVANCES the bank's least-used ordering. Lose it
            # and every deck (and the next text post) is handed the SAME anchor forever, which is
            # the failure the comment above exists to prevent.
            log_warning("Could not record the story bank entry as used — the next post will reuse "
                        "the same anchor", exc=e, user_id=user_id, post_id=post_id,
                        task_name="create_carousel_content")

    # Map stage to carousel model class
    stage_lower = (stage or "").lower()
    if "awareness" in stage_lower:
        model_cls = EducationalContentCarousel
    elif "consideration" in stage_lower:
        model_cls = CaseStudyCarousel
    elif "decision" in stage_lower:
        model_cls = ProductDemoCarousel
    else:
        model_cls = IndustryInsightsCarousel

    # Pick a template that fits the buyer stage
    _template_by_stage = {
        "awareness": "bold_listicle",
        "consideration": "step_framework",
        "decision": "stat_reveal",
        "personal": "story_arc",
        "story": "story_arc",
    }
    carousel_template = next(
        (v for k, v in _template_by_stage.items() if k in stage_lower),
        "bold_listicle",
    )

    carousel_obj = None
    try:
        carousel_obj = model_cls(**carousel_dict)
    except Exception as e:
        # ERROR, not INFO: there is no raw-slide fallback any more — a deck that will not parse
        # flags the post 'error' below and a human has to fix it. This is where the fault is
        # DETECTED and the only place the exception is in hand, so it is the one that files.
        log_error("Could not parse the generated carousel into a slide model", exc=e,
                  user_id=user_id, post_id=post_id, task_name="create_carousel_content")

    slide_urls = []
    if carousel_obj is not None and post_id is not None:
        try:
            # Render slide images using Pillow; explicit override wins over auto-pick
            image_paths = create_carousel_slide_images(
                carousel_obj, post_id, template=template or carousel_template, user_id=user_id
            )
            slide_urls = [
                f"{API_URL_FINAL}/api/assets?file_name=images/carousel/{post_id}/{os.path.basename(p)}"
                for p in image_paths
            ]
            update_db_post_carousel_slides(post_id, slide_urls)
            log_info(f"Carousel slides saved: {len(slide_urls)} images for post_id={post_id}")

            # Also generate PPTX for user download
            try:
                ppt_name = f"carousel_{post_id}"
                create_ppt(ppt_name, carousel_obj, post_id=post_id, user_id=user_id)
            except Exception as ppt_err:
                log_warning(
                    "PPTX generation failed (non-fatal)",
                    exc=ppt_err,
                    post_id=post_id,
                    user_id=user_id,
                    task_name="create_carousel_content",
                )
        except Exception as img_err:
            # ERROR, not INFO: the comment below is explicit that there is no degraded fallback —
            # the post is flagged 'error' and waits for a human. Nothing upstream logs this, so
            # without it a user's carousel fails silently and only the DB row knows.
            log_error("Carousel slide image generation failed", exc=img_err, user_id=user_id,
                      post_id=post_id, task_name="create_carousel_content")
            # Do NOT fall back to text/placeholder images — that produced carousels of a
            # single repeated default image. Flag the post 'error' for manual/dev fix.
            if post_id is not None:
                update_db_post_status(post_id, PostStatus.ERROR)
                # DEBUG: the status write is the record, and the failure already logged above.
                # One condition gets ONE record (issue #1038).
                log_debug(f"Post {post_id} flagged 'error' — carousel slide images could not be generated.")

    elif carousel_obj is None and post_id is not None:
        # Couldn't build a valid carousel model — flag for manual fix rather than degrade.
        update_db_post_status(post_id, PostStatus.ERROR)
        # DEBUG: restates the parse failure already logged at ERROR where it was detected.
        log_debug(f"Post {post_id} flagged 'error' — could not generate a valid carousel model.")

    # Same V51 shape history text posts write, so a carousel's archetype/hook is what the NEXT
    # piece rotates away from — one rotation across both, never two drifting ones.
    if post_id is not None and blueprint:
        try:
            update_db_post_shape(post_id, blueprint.get("format"), blueprint.get("hook_style"),
                                 topic=blueprint.get("subject"))
        except Exception as e:
            log_warning("Could not persist the carousel's shape — future posts will not rotate "
                        "away from it", exc=e, user_id=user_id, post_id=post_id,
                        task_name="create_carousel_content")

    return post_text


def _avatar_media_state(post_id: Optional[int], used_avatar: Optional[bool] = None) -> str:
    """Was THIS frame a real LoRA render? ``"true"`` / ``"false"`` / ``"unknown"``.

    ``used_avatar`` is the renderer's own per-render answer and always wins when it is present.
    ``posts.avatar_media`` is only the fallback, because it cannot answer the same question: it is
    a sticky per-POST flag that any earlier avatar render sets and nothing clears, so a re-render
    or a gate retry that fell back to base Flux still reads true — filing the fallback frame in the
    LoRA bucket the split exists to keep clean (issue #1430).

    It is reported as a STRING rather than folded to a boolean because an unreadable flag is not
    the same reading as a base-Flux fallback: collapsing them would put the rows that cannot be
    attributed into that same bucket.
    """
    if used_avatar is not None:
        return "true" if used_avatar else "false"
    try:
        from cqc_lem.utilities.db import post_avatar_media_state
        state = post_avatar_media_state(post_id)
    except Exception as e:
        log_debug("Could not read the avatar-media flag for the likeness probe", error=str(e),
                  post_id=post_id, action_type="avatar_likeness_probe")
        return "unknown"
    if state is None:
        return "unknown"
    return "true" if state else "false"


def _check_avatar_likeness(image_path: str, avatar: dict,
                           user_id: Optional[int] = None,
                           post_id: Optional[int] = None,
                           used_avatar: Optional[bool] = None) -> None:
    """Telemetry-only likeness probe on the stored source frame (issue #1279).

    Runs after the avatar source frame renders and before the video model sees it. The probe is
    telemetry-only by default; ``AVATAR_LIKENESS_VIDEO_HOLD_ENABLED`` makes a failed probe drop the
    AI video to the Pexels fallback. Every result is emitted to PostHog via
    ``track_avatar_likeness_probe`` so the false-positive rate can be measured before any hold is
    turned on — carrying ``used_avatar`` so a checked-negative can be attributed to a bad LoRA
    render rather than to the base-Flux fallback frame, which carries no likeness by design.

    ``used_avatar`` is the renderer's reading for the frame being probed; ``None`` falls back to
    the sticky ``posts.avatar_media`` flag (issue #1430).
    """
    if not AVATAR_LIKENESS_PROBE_ENABLED:
        return

    from cqc_lem.utilities.avatar.likeness_probe import AvatarLikenessHold, probe_avatar_likeness
    from cqc_lem.utilities.observability import track_avatar_likeness_probe

    verdict = probe_avatar_likeness(image_path, avatar, user_id=user_id, post_id=post_id)
    track_avatar_likeness_probe(user_id, post_id, verdict,
                                used_avatar=_avatar_media_state(post_id, used_avatar))
    if AVATAR_LIKENESS_VIDEO_HOLD_ENABLED and verdict.get("checked") and verdict.get("present") is False:
        # INFO, not WARNING: holding a frame the probe declined is the flag doing its job, and a
        # recurring warning is re-emitted at ERROR and filed as a grouped defect — one per held
        # video, against working behaviour. The `avatar_likeness_probe` event is the record.
        log_info(
            "Avatar likeness probe declined the source frame — dropping to stock fallback",
            user_id=user_id,
            post_id=post_id,
            action_type="avatar_likeness_probe",
        )
        raise AvatarLikenessHold("Avatar likeness probe declined the source frame")


def _post_missing_required_asset(post_id: int, post_type, video_url) -> bool:
    """True when a video post has no video or a carousel post has no slides — used to
    hold such posts PENDING instead of approving them to publish with no media.
    """
    pt = str(post_type).lower()
    if pt == PostType.VIDEO.value:
        return not video_url
    if pt in (PostType.CAROUSEL.value, PostType.DOCUMENT.value):
        from cqc_lem.utilities.db import get_post_carousel_slides
        slides = get_post_carousel_slides(post_id)
        # Missing if empty OR only text titles (no real slide images generated yet).
        return not _carousel_slides_are_real_images(slides)
    return False


def _post_used_avatar_media(post_id: Optional[int]) -> bool:
    """Did any generated media for this post come out of the avatar LoRA? (issue #744)

    Fail-soft: an unreadable flag must not cost the post its content, and the video branch still
    triggers the disclosure independently.
    """
    try:
        from cqc_lem.utilities.db import post_used_avatar_media
        return post_used_avatar_media(post_id)
    except Exception as e:
        log_warning("Could not read the avatar-media flag for disclosure", exc=e, post_id=post_id)
        return False


def _apply_ai_disclosure(content: str) -> str:
    """Append the AI-visuals disclosure line to a caption (idempotent)."""
    if not AI_DISCLOSURE_ENABLED or not content:
        return content
    marker = AI_DISCLOSURE_TEXT.strip()
    if marker and marker in content:
        return content
    return content + AI_DISCLOSURE_TEXT


def _premium_tier_for_quality(quality: str):
    """Map a post's video_quality to (model, credits, audio); None for standard/free."""
    if quality == "premium":
        return (PREMIUM_VIDEO_MODEL, PREMIUM_VIDEO_CREDITS, True)
    if quality == "premium_top":
        return (PREMIUM_TOP_VIDEO_MODEL, PREMIUM_TOP_VIDEO_CREDITS, True)
    return None


@attribute_llm_cost(FEATURE_CONTENT)
def _generate_video_src(user_id: int, text_content: str, profile, post_id: int = None):
    """Generate a video source URL honoring the post's quality tier + video credits.

    The account avatar (when active) drives the source frame on BOTH tiers so the user's
    likeness appears regardless of tier. Standard (free) = gen4_turbo image->video off that
    avatar frame (or a base Flux.1 frame when no avatar). Premium = Veo (+audio) image->video
    on the avatar image to preserve likeness, else Veo text->video. The effective tier honors
    the post's video_quality, falling back to the user's default_video_quality preference.
    Premium credits are reserved up-front and refunded on failure; falls back to standard when
    the user has no credits, and to Pexels stock on error.
    Returns the remote Runway URL (http) or a local Pexels path, or None.
    """
    from cqc_lem.utilities.ai.ai_helper import generate_post_image
    from cqc_lem.utilities.ai.video_models import is_premium, supports_audio
    from cqc_lem.utilities.avatar.guardrails import AVATAR_SURFACE_VIDEO, resolve_avatar_for
    from cqc_lem.utilities.avatar.likeness_probe import AvatarLikenessHold
    from cqc_lem.utilities.db import (
        deduct_video_credits,
        get_default_video_quality,
        get_post_video_quality,
        get_user_content_language,
        get_video_credit_balance,
        refund_video_credits,
    )
    from cqc_lem.utilities.geocoding import DEFAULT_CONTENT_LANGUAGE

    quality = get_post_video_quality(post_id) if post_id else "standard"
    # Auto-planned posts default posts.video_quality to 'standard'; honor the user's per-user
    # default_video_quality preference so they can opt their auto-generated videos up to premium
    # (credit availability is still enforced below — no credits degrades back to standard).
    if quality == "standard" and user_id:
        quality = get_default_video_quality(user_id)
    tier = _premium_tier_for_quality(quality)

    model, audio, deducted = STANDARD_VIDEO_MODEL, False, 0
    if tier and user_id:
        pmodel, pcredits, paudio = tier
        if get_video_credit_balance(user_id) >= pcredits and \
                deduct_video_credits(user_id, pcredits, post_id, reason=f"premium_video_{pmodel}"):
            model, audio, deducted = pmodel, paudio, pcredits
        else:
            log_info(f"Premium video requested but no credits for user {user_id} — using standard")

    try:
        # The avatar is resolved BEFORE the image prompt is authored: its declared subject clause
        # is what stops the prompt LLM inventing a person of a different gender for the LoRA to
        # then contradict (issue #744). None here means the guardrails said no.
        avatar = resolve_avatar_for(user_id, surface=AVATAR_SURFACE_VIDEO, post_id=post_id)
        has_avatar = avatar is not None
        # Brief the ratio this tier will actually RENDER at: the image brief composes framing for
        # the aspect ratio it is handed, so briefing 1:1 and rendering the premium source frame at
        # 9:16 asked for a square composition and cropped it vertical (issue #1141).
        source_frame_ratio = "9:16" if is_premium(model) else DEFAULT_IMAGE_RATIO
        image_prompt = get_flux_image_prompt_from_ai(text_content, profile=profile,
                                                    ratio=source_frame_ratio, avatar=avatar,
                                                    surface="video")
        # Audio-capable (premium/Veo) renders need the user's language in the prompt — Veo has no
        # language parameter and invents a voiceover otherwise (issue #548). Silent models skip
        # the lookup entirely.
        language = get_user_content_language(user_id) if supports_audio(model) else DEFAULT_CONTENT_LANGUAGE
        motion = get_runway_ml_video_prompt_from_ai(text_content, image_prompt, model=model,
                                                    language=language, user_id=user_id,
                                                    post_id=post_id)[:512]
        # Filled by the renderer with whether THIS frame came out of the LoRA — the sticky
        # posts.avatar_media flag cannot say that for a re-render or a gate retry (issue #1430).
        render_info: dict = {}
        if is_premium(model):
            if has_avatar:
                image_path = generate_post_image(image_prompt, user_id, ratio=source_frame_ratio,
                                                 surface=AVATAR_SURFACE_VIDEO, post_id=post_id,
                                                 render_info=render_info)
                _check_avatar_likeness(image_path, avatar, user_id=user_id, post_id=post_id,
                                       used_avatar=render_info.get("used_avatar"))
                src = create_runway_video(image_path, motion, model=model, ratio="9:16", audio=audio,
                                          user_id=user_id, post_id=post_id)
            else:
                # Trim the scene half, never the motion half — the motion prompt carries the audio
                # direction and a truncated one silently disables audio (issue #548).
                head = image_prompt[:max(0, 980 - len(motion) - len(" Motion: "))]
                combined = f"{head} Motion: {motion}"
                src = create_runway_video(None, combined, model=model, ratio="9:16", audio=audio,
                                          user_id=user_id, post_id=post_id)
        else:
            # Standard tier still uses the account avatar for the source frame when the user has one
            # (avatar likeness regardless of tier; premium only adds the higher-quality Veo motion +
            # credit spend). generate_post_image falls back to base Flux.1 when there is no avatar.
            if has_avatar:
                image_path = generate_post_image(image_prompt, user_id, ratio=source_frame_ratio,
                                                 surface=AVATAR_SURFACE_VIDEO, post_id=post_id,
                                                 render_info=render_info)
                _check_avatar_likeness(image_path, avatar, user_id=user_id, post_id=post_id,
                                       used_avatar=render_info.get("used_avatar"))
            else:
                from cqc_lem.utilities.ai.image_gen import render_image_from_prompt
                image_path = render_image_from_prompt(image_prompt, ratio=source_frame_ratio,
                                                      user_id=user_id, post_id=post_id,
                                                      surface="video")
            src = create_runway_video(image_path, motion, model=model, ratio=DEFAULT_VIDEO_RATIO,
                                      user_id=user_id, post_id=post_id)
        if not src:
            raise RuntimeError("no video output")
        log_info(f"_generate_video_src: model={model} audio={audio} -> {str(src)[:60]}")
        return src
    except Exception as e:
        # A likeness hold lands here only to reuse the refund + stock-fallback path below; it is a
        # decision, not a generation failure, so it never warns (issue #1279).
        if isinstance(e, AvatarLikenessHold):
            log_info("Avatar likeness hold — refunding any credits and falling back to Pexels",
                     user_id=user_id, post_id=post_id, task_name="create_video_content")
        else:
            log_warning("Video generation failed — refunding any credits and falling back to Pexels",
                        exc=e, user_id=user_id, post_id=post_id, task_name="create_video_content")
        if deducted and user_id:
            refund_video_credits(user_id, deducted, post_id)
        try:
            from cqc_lem.utilities.pexels_helper import download_pexels_video
            videos_dir = os.path.join(assets_dir, 'videos', 'pexels')
            create_folder_if_not_exists(videos_dir)
            return download_pexels_video(text_content[:50], videos_dir)
        except Exception as pe:
            # DEBUG: the OUTCOME — a video post with no asset — is already warned by the caller,
            # which holds the post PENDING with a durable missing_asset finding. A second warning
            # here forks a second grouped issue for the same lost video (issue #1038).
            log_debug(f"_generate_video_src: Pexels fallback failed ({type(pe).__name__}: {pe})")
            return None


@attribute_llm_cost(FEATURE_CONTENT)
def create_video_content(user_id: int, stage: str, post_id: int = None) -> tuple[str, str | None]:
    """Write a video post: the text first, then a video generated to match it.

    Returns:
        `(text_content, video_url)`. The URL is None when BOTH the generator and the Pexels stock
        fallback came back empty — `_generate_video_src` never raises and refunds any video
        credits it spent, so this returns usable text with no asset rather than failing.
        `_post_missing_required_asset` is what turns that into a flagged post downstream.
    """
    # Get Text Content
    text_content = create_text_post(user_id, stage, post_id=post_id)
    # Load profile once so the image prompt is brand/role-aligned
    user_profile = load_profile_for_user(user_id)
    video_url = _generate_video_src(user_id, text_content, user_profile, post_id)
    return text_content, video_url


def _probe_video_file(file_path: str) -> tuple[bool, str]:
    """Cheap presence + parse probe for a downloaded video file.

    Returns `(ok, reason)`. A zero-byte file, an unreadable path, or a non-MP4 signature fails.
    When ffprobe is available we also require a positive duration parse; ffprobe absence is a
    warning but does NOT fail the probe by itself, because the head check is deterministic and
    the feature is fail-open unless `VIDEO_PROBE_ENABLED` is ON (issue #1280).
    """
    try:
        st = os.stat(file_path)
    except (OSError, FileNotFoundError) as e:
        return False, f"file not readable: {type(e).__name__}"

    if st.st_size == 0:
        return False, "zero-byte file"
    if st.st_size < VIDEO_PROBE_MIN_SIZE_BYTES:
        return False, f"file smaller than {VIDEO_PROBE_MIN_SIZE_BYTES} bytes"

    # Deterministic MP4 head check: ISO base media files start with an ftyp box.
    try:
        with open(file_path, 'rb') as f:
            box_size_bytes = f.read(4)
            box_type = f.read(4)
        if len(box_size_bytes) < 4 or len(box_type) < 4 or box_type != b'ftyp':
            return False, "missing MP4 ftyp signature"
        box_size = int.from_bytes(box_size_bytes, 'big')
        if box_size < 8 or box_size > st.st_size:
            return False, "invalid MP4 ftyp box size"
    except Exception as e:
        return False, f"head parse error: {type(e).__name__}"

    # Best-effort ffprobe when installed. A parseable duration is extra confidence, but absence
    # only logs a warning so the probe stays fail-open.
    try:
        import shutil
        ffprobe = shutil.which("ffprobe")
        if ffprobe:
            import subprocess
            out = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", file_path],
                capture_output=True, text=True, timeout=10)
            if out.returncode == 0:
                raw = (out.stdout or "").strip()
                if raw.startswith("duration="):
                    raw = raw.split("=", 1)[1]
                try:
                    duration = float(raw)
                    if duration <= 0:
                        return False, "ffprobe reports zero/negative duration"
                except ValueError:
                    return False, "ffprobe duration unparseable"
            # Non-zero ffprobe exit is advisory: the deterministic head check already passed.
    except Exception as e:
        log_warning("ffprobe availability/duration check failed — accepting head signature",
                    exc=e, task_name="_probe_video_file")

    return True, ""


def _record_video_probe_finding(post_id: int, probe_ok: bool, reason: str,
                                user_id: Optional[int] = None, task_name: str = "") -> None:
    """Record — or clear — the malformed-media note on `posts.gate_reason` (issue #1402).

    The probe verdict is the only place the reason a video file was rejected exists: the file never
    becomes `posts.video_url`, so by the time the gate pass runs the post merely looks assetless and
    the review queue can only say "no video yet". This writes WHICH way the download was unusable
    ("zero-byte file", "missing MP4 ftyp signature") next to that hold, and clears it on the next
    passing probe so a healed post stops showing a stale reason.

    The note is ADVISORY while the probe is fail-open — `missing_asset` is already holding the post,
    so a second demoting finding would add nothing but a duplicate hold. With `VIDEO_PROBE_ENABLED`
    ON a malformed asset is a hard failure by contract, and the finding demotes with it.

    Best-effort: review UX must never cost the post, so an unreadable/unwritable reason only logs.
    """
    try:
        existing = get_post_gate_reason(post_id)
        kept = [f for f in existing if f.get("gate") != GATE_MALFORMED_ASSET]
        if probe_ok:
            if len(kept) == len(existing):
                # Nothing to clear — the happy path never writes.
                return
            findings = kept
        else:
            findings = kept + [malformed_asset_finding(PostType.VIDEO.value, reason,
                                                       demoted=VIDEO_PROBE_ENABLED)]
        update_db_post_gate_reason(post_id, findings)
    except Exception as e:
        log_warning("Could not record the video probe verdict on the post — a held video post will "
                    "not say why its file was rejected", exc=e, user_id=user_id, post_id=post_id,
                    task_name=task_name or "video_asset_probe")


def _recorded_malformed_asset_notes(post_id: int) -> list[dict]:
    """The malformed-media note the video probe recorded on this post, if any (issue #1402).

    A gate pass cannot re-derive this one: the rejected file was never stored, so all the evaluator
    sees is a post with no media. Re-read here so every pass keeps the reason alongside the
    missing-asset hold it explains. Never raises — an unreadable note only costs the explanation.
    """
    try:
        return [f for f in get_post_gate_reason(post_id) if f.get("gate") == GATE_MALFORMED_ASSET]
    except Exception as e:
        log_warning("Could not read the recorded probe verdict — this post's hold will not say why "
                    "its media file was rejected", exc=e, post_id=post_id, task_name="create_content")
        return []


def _record_post_similarity_finding(post_id: Optional[int], similarity: dict,
                                    user_id: Optional[int] = None) -> None:
    """Record — or clear — the near-duplicate verdict on `posts.gate_reason` (issue #1452).

    The generation-time gate pass cannot re-derive this one. `post_similarity_report` costs a
    `lem-embedding` batch call, and the review gate has already paid it for this draft (twice, on the
    retry path) against a post history it loaded once for the whole outermost call — neither is in
    scope by the time `_gate_findings_for_post` runs. So the verdict the review gate reached is
    written here, exactly as the video probe writes the reason a file was rejected, and re-read
    there instead of measured again.

    Written on EVERY reviewed draft, not only a failing one: a regenerated post reuses its row, so a
    verdict left behind by an earlier draft would hold a clean rewrite forever.

    Best-effort — the reason is review UX, so an unreadable/unwritable gate reason costs the hold's
    explanation, never the post.
    """
    if post_id is None:
        return
    too_similar = bool((similarity or {}).get("too_similar"))
    try:
        existing = get_post_gate_reason(post_id)
        kept = [f for f in existing if f.get("gate") != GATE_SIMILARITY]
        if not too_similar:
            if len(kept) == len(existing):
                # Nothing to clear — the common path never writes.
                return
            findings = kept
        else:
            findings = kept + [similarity_finding(similarity["score"], similarity["threshold"],
                                                  similarity["match"],
                                                  measure=similarity["measure"])]
        update_db_post_gate_reason(post_id, findings)
    except Exception as e:
        log_warning("Could not record the similarity verdict on the post — a near-duplicate draft "
                    "will not be held for review", exc=e, user_id=user_id, post_id=post_id,
                    task_name="create_text_post")


def _recorded_similarity_finding(post_id: int) -> list[dict]:
    """The near-duplicate verdict the review gate recorded on this post, if any (issue #1452).

    Read only by the generation-time gate pass. `rescore_post` measures similarity live against the
    user's edited text — that is the whole point of a re-score — so it must never pick this up as
    well, or an edit that fixed the duplication would still show the draft's original verdict.

    Never raises — an unreadable verdict only costs the hold.
    """
    try:
        return [f for f in get_post_gate_reason(post_id) if f.get("gate") == GATE_SIMILARITY]
    except Exception as e:
        log_warning("Could not read the recorded similarity verdict — a near-duplicate draft will "
                    "not be held for review", exc=e, post_id=post_id, task_name="create_content")
        return []


def _recorded_slide_slop_notes(post_id: int) -> list[dict]:
    """The slide-level slop note `_report_carousel_slide_slop` recorded on this deck (issue #1512).

    A gate pass cannot re-derive this one either: the deck is persisted as rendered slide IMAGES, so
    by the time the findings are assembled the slide text is gone. Re-read here so the note survives
    both the generation-time gate pass (which writes the post's findings after generation returns)
    and every later re-score — a re-scored caption does not change what the images say.
    Never raises — an unreadable note only costs the record.
    """
    try:
        return [f for f in get_post_gate_reason(post_id) if f.get("gate") == GATE_SLIDE_SLOP]
    except Exception as e:
        log_warning("Could not read the recorded slide-level slop lint — this deck's findings will "
                    "not name the patterns its slides carry", exc=e, post_id=post_id,
                    task_name="create_content")
        return []


def _accept_probed_video(post_id: int, video_file_path: str, video_src_url: str,
                         user_id: Optional[int] = None, task_name: str = "") -> bool:
    """Decide whether a just-downloaded video file may become the post's media (issue #1280).

    The ONE place the probe verdict is enforced, so the initial-generation path and the
    regenerate/backfill path cannot drift on what counts as usable media. Emits the
    `video_asset_probe` event for every stored video (pass/fail alike) so the metric has a
    denominator. Raises when `VIDEO_PROBE_ENABLED` is ON — a malformed asset is then a hard
    failure; otherwise the probe is advisory and a failure returns False, which every caller treats
    exactly like a failed render: the URL is not persisted, so the missing-asset gate holds the post
    and asset backfill can retry it. Either way the verdict is recorded on the post
    (`_record_video_probe_finding`), so the review queue can say why the file was rejected.
    """
    probe_ok, probe_reason = _probe_video_file(video_file_path)
    track_video_asset_probe(post_id=post_id, user_id=user_id, probe_ok=probe_ok, reason=probe_reason,
                            source="runway" if str(video_src_url).startswith("http") else "pexels")
    # The reason goes onto the post here, where it exists (issue #1402) — a passing probe clears any
    # note an earlier rejection left, so a healed post stops explaining a hold it no longer has.
    _record_video_probe_finding(post_id, probe_ok, probe_reason, user_id=user_id,
                                task_name=task_name)
    if probe_ok:
        return True
    log_warning(f"Video asset probe failed ({probe_reason}) — not persisting URL",
                user_id=user_id, post_id=post_id, task_name=task_name)
    if VIDEO_PROBE_ENABLED:
        raise RuntimeError(f"video asset probe failed: {probe_reason}")
    return False


def _caption_video_asset(post_id: int, video_file_path: str, content: Optional[str],
                         user_id: Optional[int] = None, task_name: str = "") -> None:
    """Burn the post's hook onto its rendered video for LinkedIn's muted autoplay (issue #1278).

    The ONE place a POST's video gets captioned, so the initial-generation path and the
    regenerate/backfill paths cannot drift on what a shipped video looks like. `video_captions`
    rewrites the file IN PLACE, which is why this runs before C2PA signing (a re-encode after
    signing would strip the credentials) and before `posts.video_url` is persisted — the stored
    URL then points at the video that actually ships.

    In place also means the probed file is REPLACED, so the burn is handed `_probe_video_file` and
    the re-encoded output has to pass the same check the download did (issue #1280) before it is
    allowed to become the post's media. Otherwise the only bytes LinkedIn ever receives would be
    the one version nothing verified.

    Never raises and never changes the stored URL: an off flag, an unusable hook, a missing
    ffmpeg, a failed burn or an output that fails the probe all leave the uncaptioned video
    exactly where it was.

    The avatar question is asked three-valued and answered CLOSED: an unreadable
    `posts.avatar_media` counts as avatar-led, so the frame gets the sidecar and nothing else. The
    disclosure path can afford to guess "not an avatar" (it costs a caption line); painting text
    over someone's likeness on a failed DB read cannot.
    """
    from cqc_lem.utilities.db import post_avatar_media_state, update_db_post_captions
    from cqc_lem.utilities.video_captions import apply_captions_to_video, caption_srt_asset_url

    try:
        result = apply_captions_to_video(
            video_file_path, content, post_id=post_id, user_id=user_id,
            avatar_led=post_avatar_media_state(post_id) is not False,
            validate=lambda path: _probe_video_file(path)[0])
    except Exception as e:
        # apply_captions_to_video already fails open; this only catches an import/DB fault.
        log_warning("Video captioning step could not run — shipping the video uncaptioned", exc=e,
                    user_id=user_id, post_id=post_id, task_name=task_name or "caption_video_asset")
        return

    if result.srt_path:
        # The sidecar is recorded even when the burn was skipped for an avatar-led frame: the
        # author can still attach it on LinkedIn, and it is the record of what was offered.
        update_db_post_captions(post_id, result.caption_text,
                                caption_srt_asset_url(result.srt_path))


def _record_video_asset_measures(post_id: int, video_file_path: str,
                                 user_id: Optional[int] = None,
                                 task_name: str = "") -> Optional[str]:
    """Record the stored video's duration, aspect ratio and probe state beside the file (#1517).

    The ONE place a video's asset measures become durable, so both store paths — the initial
    generation and the regenerate/backfill healer — record the same reading of the same bytes.
    Call it LAST, after captioning and C2PA signing: those rewrite the file, and the measurement
    has to describe the version that actually ships.

    Why it is recorded at all: `purge_post_assets` (#148) deletes the MP4 the moment the post
    publishes, and `auto_nightly_content_quality` scores content that has already shipped — so
    without this the nightly beat re-probes a deleted file and writes `NULL / NULL / missing` for
    every video post. `score_video_asset` reads the receipt back.

    Nothing is recorded unless the probe actually read the file: a receipt is a measurement, and an
    unmeasured video must keep reading as unmeasured rather than as a clean zero (#630). That makes
    a failure here WARN-worthy — the file just passed `_accept_probed_video`, so a probe that cannot
    read it means no video post will carry measures until someone looks (a missing `ffprobe` in the
    worker image is exactly that class of fault).

    Never raises: telemetry does not cost a user their video, which is already stored.
    """
    from cqc_lem.utilities.content_quality import VIDEO_PROBE_OK, probe_video_asset
    from cqc_lem.utilities.video_receipt import write_video_receipt

    try:
        measures = probe_video_asset(video_file_path)
        if measures.get("asset_probe") != VIDEO_PROBE_OK:
            log_warning("Could not measure the stored video — its duration and aspect ratio will "
                        f"read as unmeasured after publish ({measures.get('asset_probe')})",
                        user_id=user_id, post_id=post_id,
                        task_name=task_name or "record_video_asset_measures")
            return None
        return write_video_receipt(video_file_path, post_id, measures)
    except Exception as e:
        log_warning("Could not record the stored video's asset measures — this post's video "
                    "dimensions will be blank in content_quality_scores", exc=e, user_id=user_id,
                    post_id=post_id, task_name=task_name or "record_video_asset_measures")
        return None


def _store_video_asset(post_id: int, video_src_url: str, content: Optional[str] = None,
                       user_id: Optional[int] = None) -> Optional[str]:
    """Download a generated video into the shared assets volume, probe it, and attach C2PA
    credentials to AI output. Persist posts.video_url and return the public API asset URL only when
    the probe passes. The ONE place a regenerated video is stored — both the asset-only healer and
    the full post regenerate use it. Returns None when the file is empty or unparseable and the
    probe is advisory, leaving the post for the missing-asset gate / backfill healer.
    """
    videos_dir = os.path.join(assets_dir, 'videos', 'runwayml')
    create_folder_if_not_exists(videos_dir)
    video_file_path = save_video_url_to_dir(video_src_url, videos_dir)

    if not _accept_probed_video(post_id, video_file_path, video_src_url,
                                task_name="regenerate_post_video_task"):
        return None

    # Captions BEFORE signing: the burn re-encodes the file, which would strip C2PA credentials
    # attached first.
    _caption_video_asset(post_id, video_file_path, content, user_id=user_id,
                         task_name="regenerate_post_video_task")

    # Only AI (Runway, http) output gets C2PA AI credentials — not Pexels stock.
    if str(video_src_url).startswith("http"):
        try:
            from cqc_lem.utilities.c2pa_helper import add_ai_content_credentials
            add_ai_content_credentials(video_file_path)
        except Exception as e:
            # WARNING: c2pa_helper's own docstring promises it "never raises into the generation
            # pipeline" and logs its own skips, so anything arriving here is the best-effort module
            # itself broken — which recurs on every AI video until someone looks.
            log_warning("C2PA signing raised — the video ships without content credentials", exc=e,
                        post_id=post_id, task_name="regenerate_post_video_task")
    # Measured LAST, on the bytes that ship, and BEFORE the URL is persisted — the file is deleted
    # at publish, so this is the only moment the measurement can be taken (issue #1517).
    _record_video_asset_measures(post_id, video_file_path, user_id=user_id,
                                 task_name="regenerate_post_video_task")
    video_file_name = os.path.basename(video_file_path)
    api_video_url = f"{API_URL_FINAL}/api/assets?file_name=videos/runwayml/{video_file_name}"
    update_db_post_video_url(post_id, api_video_url)
    return api_video_url


def regenerate_video_for_post(post_id: int) -> Optional[str]:
    """Regenerate ONLY the video asset for an existing post, keeping its content.

    Uses the post's existing text content to drive the image->video pipeline
    (RunwayML, with Pexels fallback), saves the result into the shared assets
    volume, updates posts.video_url, and returns the new public asset URL
    (or None if generation failed).
    """
    text_content = get_post_content(post_id)
    if not text_content:
        log_info(f"regenerate_video_for_post: no content for post_id={post_id}")
        return None

    user_id = None
    user_profile = None
    try:
        from cqc_lem.utilities.db import get_post_user_id
        user_id = get_post_user_id(post_id)
        user_profile = load_profile_for_user(user_id)
    except Exception as e:
        log_warning("Could not resolve the post's owner or profile — regenerating the video "
                    "without either", exc=e, post_id=post_id,
                    task_name="regenerate_post_video_task")

    # Honors the post's video_quality tier + premium video credits (deduct/refund).
    video_src_url = _generate_video_src(user_id, text_content, user_profile, post_id)
    if not video_src_url:
        return None

    try:
        api_video_url = _store_video_asset(post_id, video_src_url, content=text_content,
                                           user_id=user_id)
    except Exception as e:
        # A hard probe failure (or any other storage error) should not strand the post; log it
        # and let the missing-asset gate hold it for review / backfill.
        log_warning("Could not store the regenerated video asset", exc=e,
                    post_id=post_id, task_name="regenerate_post_video_task")
        return None

    # Symmetric with regenerate_post_carousel_task: a real video now exists, so heal a non-terminal
    # post (e.g. one left in 'planning' by asset backfill) to 'approved' so it becomes visible in the
    # Review UI and can post, instead of being stranded despite a correctly-produced video.
    from cqc_lem.utilities.db import get_post_status
    if api_video_url and get_post_status(post_id) != PostStatus.POSTED.value:
        update_db_post_status(post_id, PostStatus.APPROVED)
        log_info(f"regenerate_video_for_post: post {post_id} healed → approved")
    log_info(f"regenerate_video_for_post: post_id={post_id} -> {api_video_url}")
    return api_video_url


@shared_task.task
def regenerate_post_video_task(post_id: int):
    """Celery wrapper so premium-video upgrades run async (Veo can take minutes)."""
    return regenerate_video_for_post(post_id)


def _carousel_slides_are_real_images(slides) -> bool:
    """True if carousel slides reference real images (URLs/paths), not plain text titles."""
    if not slides:
        return False
    blob = str(slides)
    return any(marker in blob for marker in ("http", "/assets", ".png", ".jpg"))


@shared_task.task
def regenerate_post_carousel_task(post_id: int):
    """Regenerate a carousel post's slide images (used by the asset-backfill safety net).

    On success (real slide images produced) it clears an 'error'/stale state back to
    'approved' so the healed carousel can post. create_carousel_content flags 'error' on
    failure, so a failed regeneration stays flagged for manual/dev attention.
    """
    from cqc_lem.utilities.db import (
        get_post_buyer_stage,
        get_post_carousel_slides,
        get_post_status,
        get_post_user_id,
        update_db_post_content,
        update_db_post_status,
    )
    user_id = get_post_user_id(post_id)
    if not user_id:
        log_info(f"regenerate_post_carousel_task: no user for post_id={post_id}")
        return None
    stage = get_post_buyer_stage(post_id) or "awareness"
    content = create_carousel_content(user_id, stage, post_id)
    if content:
        _score_and_persist_dwell(user_id, post_id, content)
        update_db_post_content(post_id, content)

    # If real slide images now exist, heal the post back to 'approved' (e.g. from 'error').
    if _carousel_slides_are_real_images(get_post_carousel_slides(post_id)):
        if get_post_status(post_id) != PostStatus.POSTED.value:
            update_db_post_status(post_id, PostStatus.APPROVED)
            log_info(f"regenerate_post_carousel_task: post {post_id} healed → approved")
    return content


def _apply_guidance_to_text_post(user_id: int, post_id: int, content: str,
                                 user_profile: LinkedInProfile, guidance: str) -> str:
    """Apply the user's free-text guidance to a generated text caption, then sanitize and repair
    deterministic CTAs that LLM rewrites can drop. Returns the caption unchanged if guidance is
    empty or the rewrite fails.
    """
    guidance = (guidance or "").strip()
    if not guidance or not content:
        return content
    try:
        prefs = get_engagement_preferences(user_id)
        synthesis = get_or_create_profile_synthesis(user_id, user_profile)
        with llm_attribution(user_id=user_id, feature=FEATURE_CONTENT):
            revised = apply_post_guidance(content, guidance, prefs=prefs, profile_synthesis=synthesis)
        if revised:
            content = sanitize_for_linkedin(revised).strip()
            # The guidance rewrite is another LLM pass that can drop the comment-keyword
            # mechanic create_text_post already verified — re-verify after it.
            content = ensure_lead_magnet_cta(content, get_lead_magnet_settings(user_id), post_id,
                                             use_emojis=bool((prefs or {}).get("use_emojis")))
    except Exception as e:
        # WARNING: the user typed a revision request and it was silently not applied — they get a
        # regenerated post that ignored what they asked for.
        log_warning("Could not apply the user's guidance to the regenerated post — keeping the "
                    "base regeneration", exc=e, user_id=user_id, post_id=post_id,
                    task_name="regenerate_post")
    return content


def _post_is_flagged_error(post_id: int) -> bool:
    """Did the media step just flag this post 'error'? Never raises — an unreadable status falls
    back to the normal PENDING reset rather than stranding a good post.
    """
    try:
        from cqc_lem.utilities.db import get_post_status
        return get_post_status(post_id) == PostStatus.ERROR.value
    except Exception as e:
        log_warning("Could not read the post's status — falling back to the normal PENDING reset",
                    exc=e, post_id=post_id, task_name="regenerate_post")
        return False


def _finish_regenerated_post(user_id: int, post_id: int, content: str,
                             post_type_value: str, video_url: Optional[str] = None,
                             ai_media: bool = False) -> str:
    """Shared close-out for a regenerated post: disclose AI visuals, persist content, re-score
    dwell, refresh gate findings, and reset the post to PENDING for re-review. Returns the content
    as PERSISTED — the caller must return this, not its pre-disclosure copy.
    """
    # Regeneration replaces the whole caption, so the old draft's disclosure line went with it —
    # re-apply it here or a regenerated video/avatar post ships undisclosed (issue #744).
    if ai_media or _post_used_avatar_media(post_id):
        content = _apply_ai_disclosure(content)
    _score_and_persist_dwell(user_id, post_id, content)
    update_db_post_content(post_id, content)
    _persist_gate_findings(user_id, post_id,
                           _gate_findings_for_post(user_id, post_id, content, post_type_value, video_url))
    # create_carousel_content flags a slide-render failure as ERROR so it gets a manual fix; resetting
    # that to PENDING would hide a deck carrying the PREVIOUS draft's slides behind a normal-looking
    # review row (the missing-asset gate reads those stale slides as present).
    if _post_is_flagged_error(post_id):
        log_info(f"regenerate_post: post {post_id} left 'error' — its media could not be regenerated")
        return content
    update_db_post_status(post_id, PostStatus.PENDING)
    return content


def regenerate_post(post_id: int, guidance: str = None) -> Optional[str]:
    """Regenerate ONE post in place through the normal generation pipeline — so it honors the
    user's saved engagement settings (voice/tone, focus/goals, emoji/hashtag prefs, anti-self-promo)
    — then folds in optional free-text `guidance`. Mirrors regenerate_newsletter_edition. Resets
    the post to PENDING so the user re-reviews. Works for text, carousel, document, and video posts
    (issue #794).
    """
    from cqc_lem.utilities.db import get_post_buyer_stage, get_post_type, get_post_user_id

    user_id = get_post_user_id(post_id)
    if not user_id:
        log_info(f"regenerate_post: no user for post_id={post_id}")
        return None
    post_type = get_post_type(post_id)
    pt_value = post_type.value if isinstance(post_type, PostType) else post_type
    if not pt_value:
        log_info(f"regenerate_post: post {post_id} has no post_type — skipping regenerate")
        return None

    stage = get_post_buyer_stage(post_id) or "awareness"
    # Cached/DB-backed profile (no Selenium) — same source the newsletter regenerate uses.
    user_profile = load_profile_for_user(user_id)

    if pt_value == PostType.TEXT.value:
        content = create_text_post(user_id, stage, user_profile=user_profile, post_id=post_id,
                                   content_mix=_post_content_mix(post_id))
        if not content:
            log_info(f"regenerate_post: text generation produced nothing for post {post_id}")
            return None
        content = _apply_guidance_to_text_post(user_id, post_id, content, user_profile, guidance)
        content = _finish_regenerated_post(user_id, post_id, content, pt_value)
        log_info(f"regenerate_post: text post {post_id} regenerated → pending")
        return content

    if pt_value in (PostType.CAROUSEL.value, PostType.DOCUMENT.value):
        # Guidance goes INTO the deck's generation, not a caption-only rewrite after it: on a
        # carousel/document the slides ARE the post, so steering only the caption would leave the
        # user's suggestions off the thing they were looking at.
        content = create_carousel_content(user_id, stage, post_id, guidance=guidance)
        if not content:
            log_info(f"regenerate_post: carousel generation produced nothing for post {post_id}")
            return None
        content = _finish_regenerated_post(user_id, post_id, content, pt_value)
        log_info(f"regenerate_post: carousel/document post {post_id} regenerated")
        return content

    if pt_value == PostType.VIDEO.value:
        content = create_text_post(user_id, stage, user_profile=user_profile, post_id=post_id,
                                   content_mix=_post_content_mix(post_id))
        if not content:
            log_info(f"regenerate_post: video caption generation produced nothing for post {post_id}")
            return None
        content = _apply_guidance_to_text_post(user_id, post_id, content, user_profile, guidance)

        # The new caption drives the new video asset so the visuals match the words (issue #794).
        video_src_url = _generate_video_src(user_id, content, user_profile, post_id)
        api_video_url = None
        if video_src_url:
            try:
                api_video_url = _store_video_asset(post_id, video_src_url, content=content,
                                                   user_id=user_id)
            except Exception as e:
                # Losing the download must not lose the regenerated caption too — persist it and
                # let the missing-asset gate hold the post, exactly as a failed render does.
                log_warning("Could not store the regenerated video asset", exc=e,
                            user_id=user_id, post_id=post_id, task_name="regenerate_post")
        # No video asset means the post is held for review with a missing_asset finding.
        content = _finish_regenerated_post(
            user_id, post_id, content, pt_value, api_video_url,
            ai_media=bool(api_video_url) and str(video_src_url).startswith("http"))
        log_info(f"regenerate_post: video post {post_id} regenerated"
                f"{'' if api_video_url else ' (caption only — video asset failed)'}")
        return content

    log_info(f"regenerate_post: post {post_id} has unsupported post_type '{pt_value}' — skipping")
    return None


@shared_task.task
def regenerate_post_task(post_id: int, guidance: str = None):
    """Celery wrapper so post regeneration (slow lem-complex call) runs async off the API request."""
    return regenerate_post(post_id, guidance)


# --- Occasion / milestone drafts (issue #1074) -------------------------------------------------
# LinkedIn's native "Celebrate an occasion" composer has no API entity, so LEM writes the copy and
# the author pastes it in. Everything below is the NORMAL generation pipeline with two things
# pinned: the archetype (the human named the event, so the shape is not up for rotation) and the
# factual anchor (what they typed is the ONLY specific the writer may state about it).

# The story-bank `kind` each occasion archetype's anchor is presented as — the bank's directive is
# already "this is the post's factual anchor, invent nothing else", which is exactly the contract an
# occasion announcement needs, so the occasion rides that directive rather than a parallel one.
_OCCASION_STORY_KINDS: dict = {
    "project_launch": "artifact",
    "educational_milestone": "anecdote",
}


def draft_occasion_post(post_id: int, archetype: str, occasion: str) -> Optional[str]:
    """Write the copy for ONE occasion/milestone post and leave it PENDING for review.

    `occasion` is the author's own description of the real event; it becomes the writer's ONLY
    allowed specific about it (the story-bank no-fabrication contract), so an empty one is refused
    rather than handed to the model to fill in. The row keeps `manual_publish`, so a draft that
    lands here never reaches the publish path — the author copies it into LinkedIn's occasion
    composer. Returns the persisted content, or None when nothing could be written.
    """
    from cqc_lem.utilities.db import get_post_buyer_stage, get_post_user_id

    occasion = (occasion or "").strip()
    archetype = (archetype or "").strip()
    if not occasion or archetype not in _OCCASION_STORY_KINDS:
        log_error("Occasion post has no usable event description or archetype",
                  post_id=post_id, task_name="draft_occasion_post")
        return None

    user_id = get_post_user_id(post_id)
    if not user_id:
        log_info(f"draft_occasion_post: no user for post_id={post_id}")
        return None

    # The occasion IS the anchor: a synthetic bank entry so the writer gets the bank's absolute
    # "these facts are the ONLY personal specifics you may state" rule for free.
    entry = {"kind": _OCCASION_STORY_KINDS[archetype], "body": occasion}
    blueprint = _select_post_blueprint(user_id, preferred_formats=[archetype])
    blueprint = {**blueprint, "subject": occasion[:200],
                 "fact_anchors": _story_bank.fact_sources(entry)}

    stage = get_post_buyer_stage(post_id) or occasion_stage("post", archetype)
    content = create_text_post(
        user_id, stage, post_type="personal_story",
        user_profile=load_profile_for_user(user_id),
        blueprint=blueprint, post_id=post_id,
        # No 70/20/10 class: an occasion is seeded by a real event, not by the plan's cadence, so
        # counting it into the mix would move the governor's ratios for something it never planned.
        story_directive=_story_bank.story_directive(entry))
    if not content:
        log_error("Occasion post generation produced nothing", user_id=user_id, post_id=post_id,
                  task_name="draft_occasion_post")
        update_db_post_status(post_id, PostStatus.ERROR)
        return None

    content = _finish_regenerated_post(user_id, post_id, content, PostType.TEXT.value)
    log_info(f"Occasion post {post_id} drafted ({archetype}) — publish natively",
             user_id=user_id, post_id=post_id, task_name="draft_occasion_post")
    return content


@shared_task.task
def draft_occasion_post_task(post_id: int, archetype: str, occasion: str):
    """Celery wrapper so occasion drafting (a slow lem-complex call) runs off the API request."""
    return draft_occasion_post(post_id, archetype, occasion)


def _post_content_mix(post_id: int) -> Optional[str]:
    """The post's assigned 70/20/10 class — a regenerate has to keep it or the plan's mix silently
    drifts. Best-effort: an unreadable class only costs the mix steering, never the regenerate.
    """
    try:
        return get_post_content_mix(post_id)
    except Exception as e:
        log_warning("Could not read the post's 70/20/10 mix class — the plan's mix will drift",
                    exc=e, post_id=post_id, task_name="regenerate_post")
        return None


def _check_post_alignment(content: str, prefs: dict, user_id: int = None, post_id: int = None,
                          user_profile: LinkedInProfile = None,
                          profile_synthesis: Optional[str] = None) -> bool:
    """Topic Authority (Topic DNA) governor on a FINAL post (issue #384): when the user declared
    focus_topics, score how tightly the post sits inside their niche — their focus topics PLUS the
    profile headline/about vocabulary — via the cheap deterministic overlap heuristic
    (topic_authority_score — NO LLM call). Below the off-niche threshold, POST_ALIGNMENT_LLM_CHECK_ENABLED
    (default OFF, the COMMENT_RESEARCH_ENABLED cost-gating pattern) lets an LLM relevance check rescue
    keyword misses — it only fires when the heuristic already failed, so the default path costs
    nothing. An off-niche post logs a structured warning (with its score) and never blocks the
    pipeline.
    """
    topics = [str(t).strip() for t in ((prefs or {}).get("focus_topics") or []) if str(t).strip()]
    if not topics or not content:
        return True
    headline, about = profile_topic_dna(user_profile, profile_synthesis)
    threshold = topic_authority_min()
    score = topic_authority_score(content, topics, headline, about)
    aligned = score >= threshold
    if not aligned and str(os.getenv("POST_ALIGNMENT_LLM_CHECK_ENABLED", "")).strip().lower() in (
            "1", "true", "yes", "on"):
        from cqc_lem.utilities.ai.ai_helper import post_is_relevant
        aligned = post_is_relevant(content, topics)
    if not aligned:
        log_warning("Generated post is off-niche vs the user's Topic DNA — does not relate to any "
                    "declared focus topic",
                    user_id=user_id, post_id=post_id, task_name="create_text_post",
                    topic_authority_score=round(score, 4), topic_authority_min=round(threshold, 4))
    return aligned


def _proof_regen_enabled() -> bool:
    """Whether a draft missing its A2 first-person proof slot is regenerated (vs. only logged). Read
    at call time (the POST_SIMILARITY_MAX / research-toggle live-env pattern) so ops can dial the
    extra generation cost back without a restart. Defaults ON — the reject/regenerate is the point of
    issue #383; the detector still runs and warns even when regeneration is off, so the A1/anti-slop
    signal is never lost.
    """
    return str(os.getenv("POST_PROOF_REGEN_ENABLED", "on")).strip().lower() in (
        "1", "true", "yes", "on")


def _fabrication_regen_enabled() -> bool:
    """Whether a draft that states a first-person specific we never gave it is regenerated (vs. only
    logged). Same live-env pattern as POST_PROOF_REGEN_ENABLED; defaults ON because a fabricated
    number about the author is the one failure the story bank exists to prevent (issues #620/#416).
    Only ever consulted when a story entry WAS selected — without one there is no allow-list.
    """
    return str(os.getenv("POST_FABRICATION_REGEN_ENABLED", "on")).strip().lower() in (
        "1", "true", "yes", "on")


def _select_story_for_post(user_id: int, prefs: dict = None,
                           blueprint: dict = None) -> Optional[dict]:
    """The one story-bank entry this post is anchored to, or None when the bank can't ground it
    (empty, all retired, or nothing related to the user's focus topics). Never fatal — a DB hiccup
    just falls back to the no-fabrication directive.
    """
    try:
        entries = get_story_bank_entries(user_id, active_only=True)
    except Exception as e:
        log_warning("Story bank unreadable — writing this post without a fact anchor", exc=e,
                    user_id=user_id, task_name="create_text_post")
        return None
    topics = [str(t).strip() for t in ((prefs or {}).get("focus_topics") or []) if str(t).strip()]
    return _story_bank.select_story(entries, subject=(blueprint or {}).get("subject"),
                                    focus_topics=topics)


def _score_and_persist_authenticity(user_id: int, post_id: int, content: str,
                                    user_profile: LinkedInProfile, profile_synthesis: str = None,
                                    prefs: dict = None,
                                    task_name: str = "create_text_post") -> None:
    """Run the authenticity gate's LLM judge on a finished draft and persist the 0-100 score (issue
    #382). This only SCORES + records; the demotion to PENDING happens in the content-plan
    status-setter, which reads the score back. No-op when the gate is disabled. Best-effort — a scorer
    or DB hiccup never blocks generation (score_authenticity itself fails open).

    `task_name` names the caller on the emitted logs — the deck caption is judged from
    `create_carousel_content` (issue #1512), and a carousel's low-score warning must not read as a
    text post's.
    """
    if not authenticity_gate_enabled():
        return
    try:
        result = score_authenticity(content, profile=user_profile,
                                    profile_synthesis=profile_synthesis, prefs=prefs)
        update_db_post_authenticity_score(post_id, result.get("score"))
        if result.get("flagged"):
            log_warning(
                f"Post flagged as low-authenticity (score {result.get('score')} < "
                f"{authenticity_score_min(prefs)}) — will hold for review: "
                f"{'; '.join(result.get('reasons') or []) or 'no reasons given'}",
                user_id=user_id, post_id=post_id, task_name=task_name)
        else:
            log_info(f"Post authenticity score {result.get('score')}",
                     user_id=user_id, post_id=post_id, task_name=task_name)
    except Exception as e:
        # WARNING: score_authenticity already fails open on its own, so anything raising here means
        # the gate silently did not run and the draft auto-approves unscored.
        log_warning("Authenticity scoring raised — this post is not scored by the gate", exc=e,
                    user_id=user_id, post_id=post_id, task_name=task_name)


def _is_affiliate_promo(content: str, post_id: Optional[int]) -> bool:
    """Whether this draft is affiliate promotion — scoped to the post's OWN author's referral code,
    the same way the publish gate grades it, so a post that quotes somebody else's link is not held
    as if it were this author's paid endorsement. Unreadable ownership reads as 'not affiliate': the
    hold is a review courtesy, and the publish gate is what actually enforces the disclosure.
    """
    try:
        from cqc_lem.utilities.db import get_post_user_id
        from cqc_lem.utilities.marketing.affiliate import is_affiliate_content
        owner = get_post_user_id(post_id) if post_id is not None else None
        # An owner we could not resolve must NOT fall through to `user_id=None`, which matches ANY
        # member's referral code: that would hold somebody's post with "this is your paid
        # endorsement" over a link that is not theirs.
        if owner is None:
            return False
        return is_affiliate_content(content, user_id=owner)
    except Exception as e:
        log_warning("Could not evaluate whether this post is affiliate promotion", exc=e,
                    post_id=post_id, task_name="create_content")
        return False


def evaluate_post_gates(post_id: int, content: str, post_type: Union[PostType, str, None],
                        video_url: Optional[str] = None,
                        engagement_prefs: Optional[dict] = None,
                        authenticity_score: Optional[int] = None,
                        recent_texts: Optional[list[str]] = None,
                        user_profile: Optional[LinkedInProfile] = None,
                        profile_synthesis: Optional[str] = None,
                        archetype: Optional[str] = None,
                        fact_anchors: Optional[list] = None,
                        author_edited: bool = False,
                        cta_keyword: Optional[str] = None) -> list[dict]:
    """Run the quality gates over a FINISHED post and return their structured findings (issue #421).

    One evaluator for both callers: the content-plan status-setter (which knows the freshly persisted
    authenticity score) and the re-score endpoint (which additionally supplies the user's post history
    and profile, so the near-duplicate and focus-alignment gates can run against an edited draft).
    A gate whose inputs are absent is skipped, never guessed. Every finding carries the score it
    failed on, the threshold it failed against, and what the user can do about it.
    """
    findings = []
    prefs = engagement_prefs or {}

    if _post_missing_required_asset(post_id, post_type, video_url):
        findings.append(missing_asset_finding(post_type))
        # WHY the media is missing, when the video probe rejected a downloaded file (issue #1402).
        # Carried only while the missing-asset hold stands: once real media exists the note is
        # stale, and the next passing probe clears it from the post anyway.
        findings.extend(_recorded_malformed_asset_notes(post_id))

    if authenticity_score is not None and authenticity_gate_enabled():
        threshold = authenticity_score_min(prefs)
        if authenticity_score < threshold:
            findings.append(authenticity_finding(authenticity_score, threshold))

    # Artifact-CTA lint (issue #618): runs for EVERY post type — the deterministic repair in
    # create_text_post only covers text posts, and a user can edit a meeting ask back in.
    if content and contains_meeting_ask(content):
        findings.append(meeting_cta_finding(meeting_ask_excerpts(content)))

    # Affiliate promotion (issue #770): a paid endorsement under the author's name is never
    # auto-scheduled, however well it scores. Derived from the CONTENT (the referral link), not from
    # which generator wrote it, so a post the user pasted their own link into is held too.
    if content and _is_affiliate_promo(content, post_id):
        from cqc_lem.utilities.marketing.affiliate import disclosure_text
        findings.append(affiliate_promo_finding(disclosure_text()))

    # Deterministic AI-slop lint (issue #625 / D1): the regeneration in `_review_generated_post`
    # gets first crack at these; anything still standing here HOLDS the post, with the exact
    # constructions named. Warn-severity checks ride along as advisory detail and never demote.
    if content:
        slop = slop_lint_report(content, "post", exempt_keyword=cta_keyword)
        if not slop["passes"]:
            findings.append(slop_finding(violation_reasons(slop["hard"]),
                                         violation_reasons(slop["warnings"])))

    # Slide-level AI-slop note for a deck (issue #1512), measured at generation and re-read here —
    # the slide text no longer exists by this point. Advisory as recorded, so it never demotes.
    if str(post_type).lower() in (PostType.CAROUSEL.value, PostType.DOCUMENT.value):
        findings.extend(_recorded_slide_slop_notes(post_id))

    if content and recent_texts:
        # Embedding-first (issue #1265) — the finding carries the measure that fired, because a
        # cosine score and a token-overlap score are not readable against each other.
        sim = post_similarity_report(content, recent_texts, prefs)
        if sim["too_similar"]:
            findings.append(similarity_finding(sim["score"], sim["threshold"], sim["match"],
                                               measure=sim["measure"]))

    # No-fabrication guard (issue #619 / G4): only the save-targeted archetypes whose value IS the
    # specifics. A draft that invented a number is held; so is one that honestly deferred its
    # numbers to placeholders — those need the author's real figures before it can publish.
    # `author_edited` marks a re-score of text a HUMAN has since edited: the guard exists to stop
    # the MODEL inventing specifics, so the author's own numbers are verified by definition and only
    # unfilled placeholders can still hold the post — otherwise filling them in could never clear it.
    if content and requires_fact_anchor("post", archetype):
        report = fact_grounding_report(content, fact_anchors)
        unverified = [] if author_edited else report["unverified_values"]
        if unverified or report["placeholders"]:
            findings.append(fact_grounding_finding(unverified, report["placeholders"]))

    topics = [str(t).strip() for t in (prefs.get("focus_topics") or []) if str(t).strip()]
    if content and topics:
        headline, about = profile_topic_dna(user_profile, profile_synthesis)
        threshold = topic_authority_min()
        score = topic_authority_score(content, topics, headline, about)
        if score < threshold:
            findings.append(focus_finding(score, threshold, topics))

    return findings


def _gate_findings_for_post(user_id: int, post_id: int, content: str,
                            post_type: Union[PostType, str, None],
                            video_url: Optional[str] = None) -> list[dict]:
    """The generation-time gate pass: the gates that can hold a fresh draft (media + authenticity +
    the near-duplicate verdict the review gate already reached), evaluated against the user's own
    thresholds. Best-effort — the reason is review UX, so a prefs or score read that fails costs the
    explanation, never the post.

    Similarity arrives as a RECORDED verdict rather than a live measurement (issue #1452): the post
    history and the embedding call both belong to the review gate inside `create_text_post`, so
    re-running the gate here would be a second `lem-embedding` call and a second history read for
    one draft. `evaluate_post_gates` is therefore still handed no `recent_texts` from this caller.
    """
    try:
        score = get_post_authenticity_score(post_id)
    except Exception as e:
        # An unreadable score only silences THAT gate — the media check still has to run, or a
        # DB hiccup would let an assetless video post auto-approve. WARNING to match the sibling
        # handler ten lines below, which has always logged this same shape at WARNING.
        log_warning("Could not read the authenticity score — that gate is skipped for this post",
                    exc=e, user_id=user_id, post_id=post_id, task_name="create_content")
        score = None
    archetype = _post_archetype_or_none(post_id)
    similarity = _recorded_similarity_finding(post_id)
    try:
        return evaluate_post_gates(
            post_id, content, post_type, video_url,
            engagement_prefs=_engagement_prefs_or_empty(user_id),
            authenticity_score=score,
            archetype=archetype,
            fact_anchors=_fact_anchors_for(user_id, archetype),
            cta_keyword=_cta_keyword_for(user_id, post_id)) + similarity
    except Exception as e:
        log_warning("Could not evaluate the quality gates for this post", exc=e,
                    user_id=user_id, post_id=post_id, task_name="create_content")
        return similarity


def _cta_keyword_for(user_id: int, post_id: int) -> Optional[str]:
    """The lead-magnet trigger word this post is allowed to ask for, so the slop lint's bait check
    exempts a sanctioned "Comment KEYWORD" CTA (the same exemption `strip_engagement_bait` makes at
    generation time). Never raises — an unreadable setting just means no exemption.
    """
    try:
        lead_magnet = get_lead_magnet_settings(user_id)
    except Exception as e:
        log_warning("Could not read the lead-magnet settings — the slop lint will not exempt this "
                    "post's sanctioned CTA", exc=e, user_id=user_id, post_id=post_id,
                    task_name="create_content")
        return None
    if not lead_magnet or not should_include_lead_magnet_cta(lead_magnet, post_id):
        return None
    return str(lead_magnet.get("keyword") or "").strip() or None


def _post_archetype_or_none(post_id: int) -> Optional[str]:
    """The post's assigned archetype (V51), never raising — an unreadable one just means the
    archetype-specific gates are skipped for this pass, exactly like an unreadable score does.
    """
    try:
        from cqc_lem.utilities.db import get_post_archetype
        return get_post_archetype(post_id)
    except Exception as e:
        log_warning("Could not read the post's archetype — the archetype-specific gates are "
                    "skipped for this pass", exc=e, post_id=post_id, task_name="create_content")
        return None


def _persist_gate_findings(user_id: int, post_id: int, findings: list[dict]) -> None:
    """Best-effort write of the gate findings onto the post (issue #421). The reason is review UX —
    losing it must never fail generation or a re-score.
    """
    try:
        update_db_post_gate_reason(post_id, findings)
    except Exception as e:
        log_warning("Could not persist the gate findings — a held post will show no reason", exc=e,
                    user_id=user_id, post_id=post_id, task_name="create_content")


def _engagement_prefs_or_empty(user_id: int) -> dict:
    """Engagement preferences for threshold resolution, never raising — a prefs read that fails just
    means the gates fall back to the deploy-wide defaults.
    """
    try:
        return get_engagement_preferences(user_id) or {}
    except Exception as e:
        log_warning("Could not load engagement preferences — the gates fall back to the deploy-wide "
                    "defaults", exc=e, user_id=user_id, task_name="create_content")
        return {}


def rescore_post(post_id: int) -> dict:
    """Re-run the quality gates on a post's CURRENT (possibly user-edited) content and update its
    hold (issue #421). This is the 'edit & re-score' half of the review flow: a draft that now passes
    every gate is promoted PENDING -> APPROVED without a full regenerate, and one that still fails
    keeps a fresh, specific reason. Honors the user's auto-schedule preference — when they review
    every post by hand, a passing re-score clears the reason but leaves the post PENDING for them.

    Returns {passed, status, authenticity_score, findings, detail} — never raises for a missing post,
    the caller turns that into a 404.
    """
    from cqc_lem.utilities.db import get_post_status, get_post_type, get_post_user_id, get_post_video_url

    user_id = get_post_user_id(post_id)
    content = get_post_content(post_id)
    if not user_id or not content:
        return {"passed": False, "status": None, "authenticity_score": None, "findings": [],
                "detail": "Post has no content to score"}

    post_type = get_post_type(post_id)
    post_type = post_type.value if isinstance(post_type, PostType) else post_type
    prefs = _engagement_prefs_or_empty(user_id)
    user_profile = load_profile_for_user(user_id)
    try:
        profile_synthesis = get_or_create_profile_synthesis(user_id, user_profile)
    except Exception as e:
        log_warning("Could not load the profile synthesis — re-scoring without the user's voice",
                    exc=e, user_id=user_id, post_id=post_id, task_name="rescore_post")
        profile_synthesis = None

    # Re-judge authenticity against the edited text — a stale score would let an unchanged hold
    # survive a genuine rewrite (and vice versa).
    _score_and_persist_authenticity(user_id, post_id, content, user_profile, profile_synthesis, prefs)
    score = get_post_authenticity_score(post_id)

    archetype = _post_archetype_or_none(post_id)
    findings = evaluate_post_gates(
        post_id, content, post_type, get_post_video_url(post_id), engagement_prefs=prefs,
        authenticity_score=score,
        # The post is already saved, so it would match ITSELF at 100% without this exclusion.
        recent_texts=get_recent_post_texts(user_id, exclude_post_id=post_id),
        user_profile=user_profile, profile_synthesis=profile_synthesis,
        # Re-runs the no-fabrication guard against the EDITED text, so filling the placeholders in
        # with real numbers is what clears the hold.
        archetype=archetype, fact_anchors=_fact_anchors_for(user_id, archetype),
        author_edited=True, cta_keyword=_cta_keyword_for(user_id, post_id))
    _persist_gate_findings(user_id, post_id, findings)

    passed = not demoting_findings(findings)
    status = get_post_status(post_id)
    detail = "Still held for review" if not passed else "Passed every quality gate"
    if passed and status == PostStatus.PENDING.value:
        auto_schedule = bool((get_user_preferences(user_id) or {}).get("auto_schedule_posts", True))
        if auto_schedule:
            update_db_post_status(post_id, PostStatus.APPROVED)
            status = PostStatus.APPROVED.value
            detail = "Passed every quality gate — approved and queued"
        else:
            detail = ("Passed every quality gate — approve it when you're ready "
                      "(auto-scheduling is off)")
    log_info(f"Re-scored post {post_id}: {detail}", user_id=user_id, post_id=post_id,
             task_name="rescore_post")
    return {"passed": passed, "status": status, "authenticity_score": score,
            "findings": findings, "detail": detail}


def _score_and_persist_dwell(user_id: int, post_id: int, content: str) -> Optional[int]:
    """Compute the deterministic dwell-proxy score for a finished post and persist it next to the
    authenticity score (issue #391 — C2). No LLM, no gate: dwell is the dominant 2026 ranking signal
    but our score is a heuristic proxy, so a weak one only logs the specific structural reasons for
    review. Best-effort — a DB hiccup never blocks the pipeline.
    """
    if not content or post_id is None:
        return None
    try:
        report = dwell_report(content)
        score = report["score"]
        update_db_post_dwell_score(post_id, score)
        metrics = report["metrics"]
        min_score = dwell_score_min()
        if score < min_score:
            log_warning(f"Post dwell-proxy score {score} < {min_score}: "
                        f"{'; '.join(report['issues']) or 'no specific issues'}",
                        user_id=user_id, post_id=post_id, task_name="create_content")
        else:
            log_info(f"Post dwell-proxy score {score} "
                     f"({metrics['words']} words, ~{metrics['read_seconds']}s read, "
                     f"{metrics['paragraphs']} paragraphs)",
                     user_id=user_id, post_id=post_id, task_name="create_content")
        return score
    except Exception as e:
        log_warning("Dwell scoring raised — this post carries no dwell-proxy score", exc=e,
                    user_id=user_id, post_id=post_id, task_name="create_content")
        return None


def _fabricated_specifics(content: str, story: Optional[dict],
                          profile_synthesis: Optional[str] = None,
                          *extra_sources: Optional[str]) -> list:
    """First-person specifics in the draft that appear in NONE of the material we supplied — i.e.
    invented facts about the author (issue #620, enforcing the #416 no-fabrication rule). Only
    meaningful when a story entry was selected: with no entry there is no allow-list, so every
    number would look fabricated and the check is skipped entirely. `extra_sources` covers other
    text we legitimately handed the writer (e.g. the lead-magnet CTA directive, whose keyword or
    resource name can carry a number) so it is never flagged as invented.
    """
    if not story or not content:
        return []
    return _story_bank.unsourced_specifics(
        content, _story_bank.fact_sources(story, profile_synthesis, *extra_sources))


@llm_step("review")
def _review_generated_post(user_id: int, stage: str, post_type: str, user_profile: LinkedInProfile,
                           blueprint: dict, post_id: int, lead_magnet_cta: str, content: str,
                           recent_texts: list, prefs: dict = None,
                           profile_synthesis: Optional[str] = None,
                           story: Optional[dict] = None,
                           story_directive: Optional[str] = None,
                           content_mix: Optional[str] = None,
                           cta_keyword: Optional[str] = None) -> str:
    """The post-generation REVIEW GATE (the newsletter's dedup maturity applied to posts): compare
    the finished post against the user's recent posts with `post_similarity_report` — embedding
    cosine first, token overlap when the embedding endpoint is down (#1265) — check the A2
    personal-proof slot (a concrete first-person lived detail), AND
    check that every first-person specific traces back to the user's story-bank entry. On a
    fact-anchored archetype (#619) the numbers get a second pass too — any figure the post asserts
    that NOTHING in the user's verified bank backs. Too similar (> POST_EMBEDDING_SIMILARITY_MAX, or
    > POST_SIMILARITY_MAX on the degraded path),
    missing proof, fabricated specifics, or unverified numbers → regenerate ONCE with an explicit
    avoid/proof/no-invention directive; still failing → log a structured warning and keep the second
    attempt (never loop, never hard-block — the GATES are what hold a still-failing draft out of
    auto-publish). Also runs the cheap focus-alignment check on whatever content ships.

    The similarity verdict on the draft this returns is RECORDED on the post (issue #1452) so
    `_gate_findings_for_post` can hold a still-over draft at PENDING without paying a second
    embedding call: this is the only place that measurement exists, because the post history and the
    `post_similarity_report` call both live here.
    """
    similarity = post_similarity_report(content, recent_texts, prefs)
    score, threshold, match = similarity["score"], similarity["threshold"], similarity["match"]
    too_similar = similarity["too_similar"]
    missing_proof = not has_first_person_proof(content)
    proof_regen = missing_proof and _proof_regen_enabled()
    fabricated = _fabricated_specifics(content, story, profile_synthesis, lead_magnet_cta)
    fabrication_regen = bool(fabricated) and _fabrication_regen_enabled()
    # No-fabrication guard (issue #619 / G4) — only on the archetypes whose value IS the specifics.
    # Checked against the user's verified story bank (#620 / G5), so a real number out of their own
    # material passes and only an invented one is flagged. The story-exact check above is the
    # stricter of the two and stays responsible for first-person specifics.
    fact_anchored = requires_fact_anchor("post", (blueprint or {}).get("format"))
    anchors = _fact_anchors(user_id) if fact_anchored else []
    fact_report = fact_grounding_report(content, anchors) if fact_anchored else None
    unverified = bool(fact_report and fact_report["unverified"])
    # Deterministic slop lint (issue #625 / D1) — one regeneration here, then the `ai_slop` gate
    # holds whatever still trips it. Only HARD violations are worth a retry; the warn-severity
    # signals (burstiness, rule-of-three) are advisory and reported by the gate.
    slop = slop_lint_report(content, "post", exempt_keyword=cta_keyword)
    slopped = not slop["passes"]

    if not too_similar and not proof_regen and not fabrication_regen and not unverified and not slopped:
        if missing_proof:
            log_warning("Generated post lacks a concrete first-person lived detail (A2 proof slot)",
                        user_id=user_id, post_id=post_id, task_name="create_text_post")
        if fabricated:
            log_warning(f"Generated post states first-person specifics not in the story bank: "
                        f"{', '.join(fabricated)}",
                        user_id=user_id, post_id=post_id, task_name="create_text_post")
        _record_post_similarity_finding(post_id, similarity, user_id=user_id)
        _check_post_alignment(content, prefs, user_id, post_id, user_profile, profile_synthesis)
        return content

    reasons = []
    if too_similar:
        reasons.append(f"too similar to a recent post ({similarity['measure']} score {score:.2f} "
                       f"> max {threshold:.2f})")
    if proof_regen:
        reasons.append("missing a concrete first-person lived detail (A2 proof slot)")
    if fabrication_regen:
        reasons.append(f"states unsourced first-person specifics ({', '.join(fabricated)})")
    if unverified:
        reasons.append("states specifics no verified fact backs ("
                       + ", ".join(fact_report["unverified_values"][:5]) + ")")
    if slopped:
        reasons.append("trips the AI-slop lint (" + "; ".join(violation_reasons(slop["hard"])) + ")")
    log_info(f"Post {'; '.join(reasons)} — retrying once with an explicit avoid/proof directive")

    retry_directive = history_avoidance_directive(
        recent_texts, offending_text=match if too_similar else None)
    if proof_regen:
        retry_directive += personal_proof_directive(profile_synthesis)
    if fabrication_regen:
        retry_directive += _story_bank.fabrication_repair_directive(fabricated)
    if unverified:
        retry_directive += fact_retry_directive(fact_report)
    if slopped:
        retry_directive += slop_retry_directive(slop["hard"])
    try:
        second = create_text_post(user_id, stage, post_type, user_profile, refine_final_post=True,
                                  blueprint=blueprint, post_id=post_id,
                                  lead_magnet_cta=lead_magnet_cta,
                                  history_directive=retry_directive, similarity_check=False,
                                  story_directive=story_directive, content_mix=content_mix)
    except Exception as e:
        log_warning("Review-gate retry generation failed; keeping first draft", exc=e,
                    user_id=user_id, post_id=post_id, task_name="create_text_post")
        second = None
    if not second:
        # The first draft is what ships, so ITS verdict is the one the gate pass must read.
        _record_post_similarity_finding(post_id, similarity, user_id=user_id)
        _check_post_alignment(content, prefs, user_id, post_id, user_profile, profile_synthesis)
        return content

    # Re-graded, not re-used: the retry is a different draft, and the second embedding call only
    # happens on the retry path (a clean first draft still costs exactly one).
    second_similarity = post_similarity_report(second, recent_texts, prefs)
    _record_post_similarity_finding(post_id, second_similarity, user_id=user_id)
    if second_similarity["too_similar"]:
        log_warning(f"Post still similar to a recent post after retry "
                    f"({second_similarity['measure']} score {second_similarity['score']:.2f} > "
                    f"max {second_similarity['threshold']:.2f}); the similarity gate will hold it "
                    f"for review",
                    user_id=user_id, post_id=post_id, task_name="create_text_post")
    if not has_first_person_proof(second):
        log_warning("Post still lacks a concrete first-person lived detail after retry "
                    "(A2 proof slot); keeping second attempt",
                    user_id=user_id, post_id=post_id, task_name="create_text_post")
    still_fabricated = _fabricated_specifics(second, story, profile_synthesis, lead_magnet_cta)
    if still_fabricated:
        log_warning(f"Post still states first-person specifics not in the story bank after retry "
                    f"({', '.join(still_fabricated)}); keeping second attempt",
                    user_id=user_id, post_id=post_id, task_name="create_text_post")
    if fact_report is not None and fact_grounding_report(second, anchors)["unverified"]:
        log_warning("Post still states unverified specifics after retry — the fact-grounding gate "
                    "will hold it for review",
                    user_id=user_id, post_id=post_id, task_name="create_text_post")
    second_slop = slop_lint_report(second, "post", exempt_keyword=cta_keyword)
    if not second_slop["passes"]:
        log_warning("Post still trips the AI-slop lint after retry — the ai_slop gate will hold it "
                    "for review: " + "; ".join(violation_reasons(second_slop["hard"])),
                    user_id=user_id, post_id=post_id, task_name="create_text_post")
    _check_post_alignment(second, prefs, user_id, post_id, user_profile, profile_synthesis)
    return second


def _draft_as_other_type(ctx: PostDraftContext, post_types: list, unavailable: str) -> tuple:
    """Re-draft this post as a different type after `unavailable`'s source produced nothing.

    A user with no blog (or no sitemap) still gets their slot filled — as some other archetype, not
    as an empty post. Returns `(content, post_type)` because the type that actually shipped is what
    the review gate downstream has to be told about.

    The retry keeps the blueprint, story anchor, CTA and history directive already chosen
    (`with_post_type`), so this is the SAME planned post written from another source rather than a
    fresh one — and it stands the once-per-post gates down, which is what stops the outer call and
    the retry both refining and reviewing one draft. Three copies of that argument list used to sit
    inline; the type breaking up (issue #1217) replaces the recursion itself with a bounded loop.
    """
    post_types.remove(unavailable)
    retry = ctx.with_post_type(random.choice(post_types))
    content = create_text_post(retry.user_id, retry.stage, retry.post_type, retry.user_profile,
                               refine_final_post=retry.refine_final_post,
                               blueprint=retry.blueprint, post_id=retry.post_id,
                               lead_magnet_cta=retry.lead_magnet_cta,
                               history_directive=retry.history_directive,
                               similarity_check=retry.similarity_check,
                               story_directive=retry.story_directive,
                               content_mix=retry.content_mix)
    return content, retry.post_type


@llm_pipeline("post_generation", feature=FEATURE_CONTENT)
def create_text_post(user_id: int, stage: str, post_type: str = None, user_profile: LinkedInProfile=None,
                     refine_final_post: bool = True, blueprint: dict = None, post_id: int = None,
                     lead_magnet_cta: str = None, history_directive: str = None,
                     similarity_check: bool = True, story_directive: str = None,
                     content_mix: str = None, day_weekday: int = None):
    """Generate a text post for LinkedIn based on the user's profile, blog, or website content.

    Parameters:
    - user_id: ID of user to grab user profile
    - stage: Buyer journey stage for the post.
    - content_mix: this post's 70/20/10 class from the plan governor (issue #618). 'promo' is the
      one-in-ten soft-promo slot and is forced into a case-study shape; anything else sells nothing.
    - day_weekday: the slot's LOCAL weekday, which picks the day-type archetype family (issue #621).

    Returns:
    - str: Generated text post content.
    """
    final_content = None

    content_mix = normalize_content_mix(content_mix)

    # Define possible post types
    post_types = ["thought_leadership", "blog_summary", "website_content", "industry_news", "personal_story",
                  "engagement_prompt"]

    if post_type is None:
        # Randomly select a post type
        post_type = random.choice(post_types)

    if user_profile is None:
        user_email, user_password = get_user_password_pair_by_id(user_id)
        driver, wait = get_driver_wait_pair(session_name='Create Text Post', user_id=user_id)
        try:
            user_profile = get_my_profile(driver, wait, user_email, user_password, user_id=user_id)
        except Exception as e:
            log_info(f"Error getting user profile: {e}")
            user_profile = None
        finally:
            quit_gracefully(driver)

        # get_my_profile RETURNS None on a failed scrape as well as raising (issue #1101), and a
        # DOM change makes that the normal failure — so the fallback ladder has to cover both or
        # None reaches the generators and dies on `None.model_dump_json()`.
        if user_profile is None:
            # Prefer the user's cached DB profile (no Selenium) — the old hardcoded "John Doe,
            # Software Developer at ABC Inc." dummy leaked a tech persona into generated posts
            # whenever the live profile load hiccupped. Neutral dummy only as a last resort.
            try:
                user_profile = load_profile_for_user(user_id)
            except Exception as e2:
                # DEBUG: load_profile_for_user warns on both of its own failure paths, so a second
                # record here files a duplicate issue for one lost profile (issue #1038).
                log_debug(f"Cached profile unavailable: {e2}")
                user_profile = None
        if user_profile is None:
            user_profile = LinkedInProfile(full_name="LinkedIn Member", job_title="Professional")

    # User-declared focus topics + business/personal goals steer the post's angle and enforce the
    # anti-self-promo guardrail (see _alignment_directive) so posts don't drift into promoting
    # whatever the user is currently building.
    prefs = get_engagement_preferences(user_id)

    # Stable VOICE synthesis (cached weekly; lazily generated on first use) replaces the bloated full
    # profile JSON as the voice/credibility source — keeps posts in the user's voice without dragging
    # in their volatile recent activity / current projects (LEM-drift).
    profile_synthesis = get_or_create_profile_synthesis(user_id, user_profile)

    # Post-history dedup steering (the newsletter's avoid_subjects/avoid_openers, applied to posts):
    # recent posts' opening lines + subject fingerprints ride into the generation prompt as an
    # explicit AVOID block, and the same history feeds the pre-persist similarity gate below.
    # Recursive calls (fallbacks / the similarity retry) receive an explicit history_directive so
    # the history is fetched at most once per outermost call.
    recent_texts: list = []
    if similarity_check:
        try:
            recent_texts = get_recent_post_texts(user_id)
        except Exception as e:
            log_warning("Could not load the recent post history — writing with no dedup steering",
                        exc=e, user_id=user_id, post_id=post_id, task_name="create_text_post")
    if history_directive is None:
        history_directive = history_avoidance_directive(recent_texts)

    # Story bank (issue #620): anchor the post to ONE thing the user actually did, and make that
    # entry the ONLY personal specific the writer may state. An empty bank — or a bank with nothing
    # related to the user's focus topics — ships the explicit no-fabrication fallback instead, so a
    # missing story degrades to an industry observation rather than an invented anecdote. Recursive
    # calls (type fallbacks / the review-gate retry) receive the directive so the whole post stays
    # anchored to the same entry and the bank is read at most once per outermost call. Selected
    # BEFORE the blueprint so the promo demotion below can still steer shape selection.
    story = None
    story_selected_here = story_directive is None
    if story_selected_here:
        story = _select_story_for_post(user_id, prefs, blueprint)
        story_directive = _story_bank.story_directive(story)
        if story:
            log_info(f"Story bank anchor for post_id={post_id}: "
                    f"[{story.get('kind')}] {story.get('title')}")
        else:
            # DEBUG: an empty bank is the documented resting state for a user who has not filled
            # one in, and it fires on EVERY post they write. Same shape as db.get_ready_to_post_posts
            # logging INFO only when the list is non-empty.
            log_debug("No story bank entry available — writing a non-story archetype")

    # Integration seam (#618 x #620): the promo slot demands a case study built on ONE real outcome
    # number, but without a story-bank anchor the fabrication detector has no allow-list and is
    # skipped entirely — so the one post most likely to invent a client figure would ship unchecked.
    # A promo post the bank cannot ground is demoted to audience-value content instead (and NOT
    # forced into the case_snapshot blueprint below). The plan row keeps its 'promo' class, which
    # only makes the dashboard's mix-compliance ratio conservative.
    if story_selected_here and story is None and content_mix == ContentMix.PROMO.value:
        log_warning("Promo slot has no story-bank anchor — demoting this post to 'value' so a "
                    "case study cannot be invented", user_id=user_id, post_id=post_id,
                    task_name="create_text_post")
        content_mix = ContentMix.VALUE.value

    # ONE assigned SHAPE per post from the SHARED framework core (content_framework): rotate away
    # from this user's recently used post archetypes/hook styles (V51 history) exactly the way
    # newsletter editions rotate — chosen in code, no extra LLM call.
    # Close the feedback loop (issue #389 / B4): bias shape selection away from the user's
    # historically under-performing archetypes/hooks while keeping rotation + exploration.
    if blueprint is None:
        # The day-type calendar (issue #621) narrows the archetype menu to this slot's family;
        # rotation, recency avoidance and performance weighting still apply WITHIN it. The promo
        # slot's shape is not negotiable, though: the governor requires a case study (client as
        # hero, real outcome number), and an explicit `guidance` hint wins over the day-type family.
        day_type = day_type_for_weekday(day_weekday) if day_weekday is not None else None
        blueprint = _select_post_blueprint(
            user_id,
            guidance="case_snapshot" if content_mix == ContentMix.PROMO.value else None,
            preferred_formats=day_type_formats(day_weekday) if day_type else None)
        if day_type:
            log_info(f"Day type for weekday {day_weekday}: {day_type['label']}")
    log_info(f"Post blueprint: format={blueprint.get('format')} hook={blueprint.get('hook_style')} "
            f"cta={blueprint.get('cta_style')}")

    # The no-fabrication allow-list a fact-anchored archetype (#619) writes against is exactly the
    # entry the story bank handed us above — a number from some OTHER bank entry was never in this
    # prompt, so the writer stating one would still be inventing. Carried ON the blueprint because
    # that is what every post-prompt builder (and the recursive type fallbacks) already thread
    # through — on a copy, so a blueprint the caller owns is never mutated behind its back. Only
    # when the story was selected HERE: a recursive call was handed both, already paired.
    if story_selected_here:
        blueprint = {**blueprint,
                     "fact_anchors": _story_bank.fact_sources(story) if story else []}

    # Lead-magnet soft-ask: on a deterministic 1-in-N rotation (keyed off post_id so it's stable and
    # testable), weave the user's configured "comment KEYWORD and I'll DM it to you" CTA into the
    # post — the compliant way to share a resource and what fires the keyword listener in the
    # automation. Only SOME posts get it (all = spammy/pattern-flagged). Recursive fallbacks reuse
    # the already-computed directive so the same post stays consistent. No-op unless the user enabled
    # the lead magnet with a non-empty keyword.
    lead_magnet = None
    include_cta = False
    if lead_magnet_cta is None:
        try:
            lead_magnet = get_lead_magnet_settings(user_id)
            include_cta = should_include_lead_magnet_cta(lead_magnet, post_id)
            lead_magnet_cta = lead_magnet_cta_directive(lead_magnet, include_cta)
            if lead_magnet_cta:
                log_info(f"Lead-magnet CTA included on post_id={post_id} "
                        f"(keyword '{lead_magnet.get('keyword')}')")
        except Exception as e:
            log_warning("Could not read the lead-magnet settings — this post ships without the "
                        "comment-keyword mechanic", exc=e, user_id=user_id, post_id=post_id,
                        task_name="create_text_post")
            lead_magnet, include_cta, lead_magnet_cta = None, False, ""

    # Everything this draft is written from, settled (issue #1220). The type-fallback path below is
    # the one place that needed all of it at once and was repeating the whole argument list.
    draft = PostDraftContext(user_id=user_id, stage=stage, post_type=post_type,
                             user_profile=user_profile, prefs=prefs,
                             profile_synthesis=profile_synthesis, blueprint=blueprint,
                             post_id=post_id, lead_magnet_cta=lead_magnet_cta,
                             history_directive=history_directive, story_directive=story_directive,
                             content_mix=content_mix, refine_final_post=refine_final_post,
                             similarity_check=similarity_check)

    # Generate the post based on the selected type
    log_info(f"Creating text post of type: {post_type} for stage: {stage}")
    if post_type == "thought_leadership":
        final_content = get_thought_leadership_post_from_ai(user_profile, stage, prefs=prefs,
                                                            profile_synthesis=profile_synthesis,
                                                            blueprint=blueprint,
                                                            lead_magnet_cta=lead_magnet_cta,
                                                            post_id=post_id,
                                                            history_directive=history_directive,
                                                            story_directive=story_directive,
                                                            content_mix=content_mix,
                                                            user_id=user_id)
    elif post_type == "blog_summary":
        # Get the users blog url
        user_main_blog_url = get_user_blog_url(user_id)
        blog_post_url, blog_post_content = get_main_blog_url_content(user_main_blog_url)
        if blog_post_url and blog_post_content:
            process_selected_post(blog_post_url, blog_post_content)
            final_content = get_blog_summary_post_from_ai(blog_post_url, blog_post_content, user_profile, stage,
                                                          prefs=prefs, profile_synthesis=profile_synthesis,
                                                          blueprint=blueprint, lead_magnet_cta=lead_magnet_cta,
                                                          history_directive=history_directive,
                                                          story_directive=story_directive,
                                                          content_mix=content_mix)
        else:
            log_info("No blog post found for this user. Generating another post type")
            final_content, post_type = _draft_as_other_type(draft, post_types, "blog_summary")
    elif post_type == "website_content":
        # Get the users sitemap url
        sitemap_url = get_user_sitemap_url(user_id)
        if sitemap_url:
            content = generate_website_content_post(sitemap_url, user_profile, stage, prefs=prefs,
                                                    profile_synthesis=profile_synthesis,
                                                    blueprint=blueprint, lead_magnet_cta=lead_magnet_cta,
                                                    history_directive=history_directive,
                                                    story_directive=story_directive,
                                                    content_mix=content_mix)
            if content:
                final_content = content
            else:
                log_info("No relevant content found in the sitemap. Generating another post type")
                final_content, post_type = _draft_as_other_type(draft, post_types,
                                                                "website_content")
        else:
            log_info("No sitemap found for this user. Generating another post type")
            final_content, post_type = _draft_as_other_type(draft, post_types, "website_content")
    elif post_type == "industry_news":
        final_content = get_industry_news_post_from_ai(user_profile, stage, prefs=prefs,
                                                       profile_synthesis=profile_synthesis,
                                                       blueprint=blueprint, lead_magnet_cta=lead_magnet_cta,
                                                       post_id=post_id,
                                                       history_directive=history_directive,
                                                       story_directive=story_directive,
                                                       content_mix=content_mix,
                                                       user_id=user_id)
    elif post_type == "personal_story":
        final_content = get_personal_story_post_from_ai(user_profile, stage, prefs=prefs,
                                                        profile_synthesis=profile_synthesis,
                                                        blueprint=blueprint, lead_magnet_cta=lead_magnet_cta,
                                                        post_id=post_id,
                                                        history_directive=history_directive,
                                                        story_directive=story_directive,
                                                        content_mix=content_mix,
                                                        user_id=user_id)
    else:
        final_content = generate_engagement_prompt_post(user_profile, stage, prefs=prefs,
                                                        profile_synthesis=profile_synthesis,
                                                        blueprint=blueprint, lead_magnet_cta=lead_magnet_cta,
                                                        post_id=post_id,
                                                        history_directive=history_directive,
                                                        story_directive=story_directive,
                                                        content_mix=content_mix,
                                                        user_id=user_id)

    # The sanctioned 'comment KEYWORD' ask for THIS post: the refinement passes preserve it, the
    # bait strip exempts it, and the slop lint's bait check must not read it as engagement bait.
    _cta_kw = (str(lead_magnet.get("keyword")).strip()
               if include_cta and lead_magnet else None) or None

    if refine_final_post:
        # Both refinement passes get the user's prefs so the LLM rewrites can't re-introduce
        # emojis/hashtags the user turned off (or flatten a configured tone).
        final_content = get_ai_linked_post_refinement(final_content, prefs=prefs,
                                                      preserve_cta_keyword=_cta_kw)
        # Hook + save-worthy pass: strong first line before the '…more' fold; save-worthy framing.
        final_content = optimize_post_hook(final_content, prefs=prefs,
                                           preserve_cta_keyword=_cta_kw)
        final_content = sanitize_for_linkedin(final_content)
        # Guardrail: strip classic engagement-bait CTAs (penalized), keeping lead-magnet CTAs —
        # including one whose configured trigger word collides with the bait regex (e.g. 'YES').
        final_content = strip_engagement_bait(
            final_content,
            exempt_keyword=(lead_magnet or {}).get("keyword") if include_cta else None)
        final_content = final_content.strip()

    # Humanization pass (issue #416 — A5): the final anti-AI-tell rewrite, BEFORE the review gate
    # (generate -> A2 proof -> humanize -> review -> A1). READER mode only, never fabricates, fails
    # open. Outermost call only (mirrors the A1/review gates). The lead-magnet CTA mechanic is
    # re-verified/repaired by ensure_lead_magnet_cta below, so a rewrite that reworded it is
    # recovered deterministically.
    if final_content and refine_final_post and similarity_check:
        final_content = humanize_text(final_content, content_type="post",
                                      profile_synthesis=profile_synthesis, prefs=prefs)

    # Review gate (runs once, in the outermost call): deterministic near-duplicate check against the
    # user's recent posts with ONE avoid-directive retry, plus a cheap focus-alignment check. Never
    # a hard block — worst case it logs a warning and keeps the best attempt.
    if final_content and refine_final_post and similarity_check:
        final_content = _review_generated_post(user_id, stage, post_type, user_profile, blueprint,
                                               post_id, lead_magnet_cta, final_content,
                                               recent_texts, prefs, profile_synthesis,
                                               story=story, story_directive=story_directive,
                                               content_mix=content_mix, cta_keyword=_cta_kw)

        # Dwell shaping (issue #391 — C2): deterministic, no-LLM repair of the structural dwell
        # killer every LLM rewrite above can re-introduce — wall-of-text paragraphs. Runs on
        # whichever draft the review gate kept, reflows ONLY over-long paragraphs, and never
        # truncates, so the CTA/proof/hashtag lines survive. The hook-before-fold and read-time
        # targets are steered in the prompt (content_framework.dwell_directive) and measured into
        # the persisted dwell-proxy score when the post is saved.
        final_content = shape_for_dwell(final_content)

    # Artifact-CTA policy (issue #618): a meeting ask ("book a call", "DM me to discuss") is the CTA
    # shape 2026 demotes hardest, and every LLM pass above can write one back in even though the
    # prompt bans it. Deterministic repair, run on whatever draft the review gate kept: drop the ask
    # and — only when the user genuinely has an artifact (their lead magnet, else their newsletter) —
    # close on that instead. What survives is still lint-checked by the review gate.
    if final_content and refine_final_post and similarity_check and contains_meeting_ask(final_content):
        try:
            newsletter = get_newsletter_settings(user_id)
        except Exception as e:
            log_warning("Could not read the newsletter settings — the meeting-ask CTA has no "
                        "artifact to be replaced with", exc=e, user_id=user_id, post_id=post_id,
                        task_name="create_text_post")
            newsletter = None
        repaired = replace_meeting_ask_cta(
            final_content, lead_magnet=lead_magnet, newsletter=newsletter, post_id=post_id,
            use_emojis=bool((prefs or {}).get("use_emojis")))
        if repaired and repaired != final_content:
            log_warning("Meeting-ask CTA replaced with an artifact CTA (70/20/10 promo policy): "
                        f"{'; '.join(meeting_ask_excerpts(final_content))}",
                        user_id=user_id, post_id=post_id, task_name="create_text_post")
            final_content = repaired

    # The refinement passes above (and any review-gate retry) are LLM rewrites — verified in prod to
    # reword the "comment KEYWORD" mechanic into a generic ask or drop it entirely, which silently
    # kills the auto-DM keyword listener. Deterministic verify-and-repair runs LAST, after every
    # rewrite, so the mechanic ships on every selected post (no-op when already present; variant
    # chosen by post_id so repaired CTAs stay non-identical).
    if include_cta and final_content:
        repaired = ensure_lead_magnet_cta(final_content, lead_magnet, post_id,
                                          use_emojis=bool((prefs or {}).get("use_emojis")))
        if repaired != final_content:
            log_info("Lead-magnet CTA lost in refinement - repaired deterministically",
                     post_id=post_id, user_id=user_id, task_name="create_text_post")
            final_content = repaired

    # Authenticity gate (issue #382 — 360Brew defense): score the finished draft for generic-AI risk
    # + profile-topic consistency and persist the score. Runs LAST here, on the draft this function
    # returns (issue #1264): the review gate above regenerates the post on a similarity / A2-proof /
    # fabrication / slop failure, and its retry re-enters with `similarity_check=False` — the same
    # flag guarding this call — so scoring earlier persisted the score of a draft that was thrown
    # away. Still exactly ONE judge call per shipped post: the retry never reaches this line. The
    # gate itself is a status demotion (APPROVED -> PENDING) applied by the content-plan
    # status-setter, which reads the persisted score back — mirroring the deterministic similarity
    # gate's demote-not-block posture.
    if final_content and refine_final_post and similarity_check and post_id is not None:
        _score_and_persist_authenticity(user_id, post_id, final_content, user_profile,
                                        profile_synthesis, prefs)

    # Count the story-bank entry as used so the NEXT post rotates to different raw material. Only
    # the outermost call (the one that actually selected an entry) writes, and only when a post
    # really came out of it — a failed generation must not burn the anecdote.
    if story and final_content and story.get("id"):
        try:
            record_story_bank_use(user_id, int(story["id"]))
        except Exception as e:
            # WARNING for the same reason as the carousel's copy of this write: without it the
            # bank never advances and every post reuses the same anchor.
            log_warning("Could not record the story bank entry as used — the next post will reuse "
                        "the same anchor", exc=e, user_id=user_id, post_id=post_id,
                        task_name="create_text_post")

    # Persist the assigned shape so FUTURE posts rotate away from it (the newsletter's V50 shape
    # history, applied to posts via V51). Only the outermost call (which knows the post row) writes.
    if post_id is not None and final_content and blueprint:
        try:
            update_db_post_shape(post_id, blueprint.get("format"), blueprint.get("hook_style"),
                                 topic=blueprint.get("subject"))
        except Exception as e:
            log_warning("Could not persist the post's shape — future posts will not rotate away "
                        "from it", exc=e, user_id=user_id, post_id=post_id,
                        task_name="create_text_post")

    return final_content


def get_main_blog_url_content(blog_url):
    """Retrieve recent posts from a blog, randomly select one, and send the post content to another function.

    Parameters:
    - blog_url (str): URL of the blog's homepage.

    Returns:
    - None
    """
    # Check if the blog is a WordPress site by trying the REST API
    wp_api_url = f"{blog_url.rstrip('/')}/wp-json/wp/v2/posts"

    session, headers = get_session_for_response()

    recent_posts = None

    try:
        # Make the request
        response = session.get(wp_api_url, headers=headers, timeout=10)

        # Raise an exception for HTTP errors
        response.raise_for_status()

        if response.status_code == 200:
            # Use WordPress API to get recent posts
            recent_posts = response.json()
            log_info("WordPress blog detected. Fetching posts via API...")
        else:
            # If the API is not available, fall back to web scraping
            log_info("Non-WordPress blog detected or API unavailable. Fetching posts via web scraping...")
            recent_posts = scrape_recent_posts(blog_url)

    except requests.exceptions.HTTPError as http_err:
        log_info(f"HTTP error occurred: {http_err}")
        log_info("Falling back to web scraping...")
        # In case of any request failure, fall back to web scraping
        recent_posts = scrape_recent_posts(blog_url)
    except requests.exceptions.ConnectionError as conn_err:
        log_info(f"Connection error occurred: {conn_err}")
    except requests.exceptions.Timeout as timeout_err:
        log_info(f"Timeout error occurred: {timeout_err}")
    except requests.exceptions.RequestException as req_err:
        log_info(f"Some other request error occurred: {req_err}")
        log_info("Falling back to web scraping...")
        # In case of any request failure, fall back to web scraping
        recent_posts = scrape_recent_posts(blog_url)

    # Randomly select one post from recent posts
    if recent_posts:
        selected_post = random.choice(recent_posts)
        # myprint(f"Randomly selected post")
        post_content = selected_post.get("content", "")
        try:
            # If post_content is a string, try to parse it as JSON
            if isinstance(post_content, str):
                post_content = json.loads(post_content)

            # Check if post_content is a dictionary and contains the "rendered" key
            if isinstance(post_content, dict) and "rendered" in post_content:
                post_content = post_content["rendered"]

        except (json.JSONDecodeError, TypeError):
            # If JSON decoding fails or another error occurs, return None
            log_info(f"Error parsing post content: {post_content}")

        # myprint(f"Randomly selected post content: {post_content}")
        post_url = selected_post.get("link", blog_url)

        return post_url, post_content
    else:
        log_info("No recent posts found or unable to fetch posts.")
        return None, None


def scrape_recent_posts(blog_url):
    """Fallback method to scrape recent posts from a non-WordPress blog.

    Parameters:
    - blog_url (str): URL of the blog's homepage.

    Returns:
    - list: List of dictionaries containing 'content' and 'link' for each post.
    """
    recent_posts = []
    try:
        content = fetch_content(blog_url)

        # Parse the HTML content with BeautifulSoup
        soup = BeautifulSoup(content, "html.parser")

        # Attempt to find recent post links by typical blog post selectors
        # Adjust these selectors based on the structure of the blog
        for article in soup.select("article"):
            title_element = article.find("a", href=True)
            if title_element:
                post_url = title_element["href"]

                # if post_url is a relative URL, convert it to an absolute URL
                if not post_url.startswith("http"):
                    post_url = urlparse(blog_url).scheme + "://" + urlparse(blog_url).hostname + post_url

                # post_content = title_element.get_text(strip=True)
                post_content = fetch_content(post_url)
                recent_posts.append({
                    "link": post_url,
                    "content": post_content
                })
    except requests.RequestException as e:
        log_info(f"Error fetching or parsing blog page: {e}")

    if len(recent_posts) == 0:
        log_info(f"No recent posts found on the blog page: {blog_url}")
    else:
        log_info(f"Found {len(recent_posts)} post(s) from: {blog_url}")

    return recent_posts


def process_selected_post(url, content):
    """Process the selected post's content and URL.

    Parameters:
    - content (str): The text content of the selected post.
    - url (str): The URL of the selected post.

    Returns:
    - None
    """
    if not url:
        url = "Could not retrieve URL"
    if not content:
        content = "Could not retrieve content"

    # If content is a list joint it by space
    if isinstance(content, list):
        content = " ".join(content)

    # Placeholder for whatever processing needs to be done
    log_info(f"Selected Post URL: {url}")
    if content:
        log_info(f"Selected Post Content: {content[:200]}...")  # Print the first 200 characters for preview
    else:
        log_info("Selected Post Content: None")


@llm_step("draft")
def generate_website_content_post(sitemap_url, linked_user_profile, stage: str, prefs: dict = None,
                                  profile_synthesis: Optional[str] = None, blueprint: dict = None,
                                  lead_magnet_cta: str = None, history_directive: str = None,
                                  story_directive: str = None, content_mix: str = None):
    """Generate a post based on content found on the user's website using their sitemap url catered to readers in the desired buyers journey stage.
    Scrapes or retrieves key points from the website's sitemap.
    """
    # 1. Fetch and parse the sitemap XML
    page_urls = fetch_sitemap_urls(sitemap_url)
    log_info(f"Founds {len(page_urls)} URLs from sitemap {sitemap_url}")

    # 2. Filter URLs that are likely to contain shareable content
    relevant_urls = filter_relevant_urls(page_urls)
    log_info(f"Found {len(relevant_urls)} relevant URLs in the sitemap.")

    # 3. Randomly select a URL to gather content from
    if relevant_urls:
        content = None
        selected_url = None
        attempts = 3
        while attempts > 0 and relevant_urls:
            selected_url = random.choice(relevant_urls)
            title, content = extract_page_content(selected_url)
            if content:
                break
            else:
                relevant_urls.remove(selected_url)
                attempts -= 1
        if content is not None:
            # 4. Generate a social media post based on the extracted content
            social_media_post = get_website_content_post_from_ai(content, selected_url, linked_user_profile, stage,
                                                                 prefs=prefs, profile_synthesis=profile_synthesis,
                                                                 blueprint=blueprint, lead_magnet_cta=lead_magnet_cta,
                                                                 history_directive=history_directive,
                                                                 story_directive=story_directive,
                                                                 content_mix=content_mix)
            return social_media_post
        else:
            log_info("No content extracted from the selected URL.")
            return None
    else:
        log_info("No relevant URLs found in the sitemap.")
        return None


def get_session_for_response() -> Tuple[requests.Session, dict]:
    """A retrying `requests` Session, plus the browser-shaped headers to send with it.

    A user's own marketing site is the flakiest dependency in this pipeline and often refuses a
    default `python-requests` UA outright, hence the desktop-Chrome header set and five backed-off
    retries covering 403 and 429 as well as 5xx. Retries are restricted to HEAD/GET so nothing
    replayable is ever a write.

    Returns:
        `(session, headers)` — the headers are deliberately NOT mounted on the session, so a
        caller that forgets to pass them through to `session.get` sends the default UA and gets
        blocked by exactly the sites this exists for.
    """
    # Set up headers to simulate a browser request
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.91 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive'
    }

    # Configure retries to handle intermittent network issues gracefully
    retry_strategy = Retry(
        total=5,  # Retry 5 times
        backoff_factor=1,  # Wait progressively longer each time
        status_forcelist=[403, 429, 500, 502, 503, 504],  # Retry on these status codes
        allowed_methods=["HEAD", "GET"]
    )

    # Mount session with the retry strategy
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session, headers


# Function to make a request and return parsed HTML content
def fetch_content(url):
    """Fetch a URL's raw bytes for the blog/sitemap scrapers.

    Every `requests` failure — HTTP status, connection, timeout, anything else — is logged and
    swallowed. Source material is an ENRICHMENT for a post, so an unreachable site is meant to
    degrade the content rather than fail a generation task.

    Returns:
        `response.content` (bytes), or **None** when the fetch failed. None is not a benign empty
        string: callers hand this straight to BeautifulSoup / `ElementTree.fromstring`, both of
        which raise `TypeError` on None, and their own `except` clauses only cover
        `requests.RequestException` / `ParseError`.
    """
    session, headers = get_session_for_response()

    content = None

    try:
        # Make the request
        response = session.get(url, headers=headers, timeout=10)

        # Raise an exception for HTTP errors
        response.raise_for_status()

        content = response.content

    except requests.exceptions.HTTPError as http_err:
        log_info(f"HTTP error occurred: {http_err}")
    except requests.exceptions.ConnectionError as conn_err:
        log_info(f"Connection error occurred: {conn_err}")
    except requests.exceptions.Timeout as timeout_err:
        log_info(f"Timeout error occurred: {timeout_err}")
    except requests.exceptions.RequestException as req_err:
        log_info(f"Some other request error occurred: {req_err}")

    # Return the content
    return content


def fetch_sitemap_urls(sitemap_url):
    """Fetch and parse the sitemap XML to get a list of URLs, including any sub-sitemaps.

    Parameters:
    - sitemap_url (str): The URL of the main sitemap.

    Returns:
    - list: A list of URLs from the sitemap and any sub-sitemaps.
    """
    urls = []
    try:

        content = fetch_content(sitemap_url)

        # Parse the XML content of the sitemap
        tree = ElementTree.fromstring(content)
        namespaces = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}

        # Check if the sitemap contains sub-sitemaps or URLs
        sitemap_elements = tree.findall('.//ns:sitemap/ns:loc', namespaces)
        url_elements = tree.findall('.//ns:url', namespaces)

        # Process URLs if present
        if url_elements:
            url_lastmod_pairs = []
            for url_element in url_elements:
                loc = url_element.find('ns:loc', namespaces).text
                lastmod = url_element.find('ns:lastmod', namespaces)
                if lastmod is not None:
                    lastmod = lastmod.text
                else:
                    lastmod = '1985-03-13'  # Default to a very old date if lastmod is not present
                url_lastmod_pairs.append((loc, lastmod))

            # Sort URLs by last modified time in descending order
            url_lastmod_pairs.sort(key=lambda x: x[1], reverse=True)
            urls = [url for url, lastmod in url_lastmod_pairs]

        # Process sub-sitemaps recursively if present (except for certain sitemap URLs)
        skip_if_contains_text = ['sitemap-misc.xml', 'post_tag-sitemap.xml', 'category-sitemap.xml', 'page-sitemap.xml']
        if sitemap_elements:
            for sitemap in sitemap_elements:
                sub_sitemap_url = sitemap.text
                if any(text in sub_sitemap_url for text in skip_if_contains_text):
                    log_info(f"Skipping sub-sitemap: {sub_sitemap_url}")
                    continue
                log_info(f"Fetching sub-sitemap: {sub_sitemap_url}")
                urls.extend(fetch_sitemap_urls(sub_sitemap_url))  # Recursive call

    except requests.RequestException as e:
        log_info(f"Error fetching sitemap: {e}")
    except ElementTree.ParseError as e:
        log_info(f"Error parsing sitemap XML: {e}")

    return urls


def filter_relevant_urls(urls: list[str], by_blog_post_check=True, max_list_size=10):
    """Filter the URLs to keep only those that are likely to be a blog post or contain shareable content.
    """
    keywords = ["blog", "case-study", "testimonial", "service", "news", "about"]

    relevant_urls = []
    for url in urls:
        if (by_blog_post_check and is_blog_post_combined(url)) or (
                not by_blog_post_check and any(keyword in url for keyword in keywords)):
            relevant_urls.append(url)
            if len(relevant_urls) >= max_list_size:
                break

    return relevant_urls


def extract_page_content(page_url):
    """Extract key content from the page, such as the title, main points, or summary.
    """
    try:
        content = fetch_content(page_url)

        # Parse the HTML content
        soup = BeautifulSoup(content, "html.parser")

        # Extract the page title
        title = soup.title.string if soup.title else "Untitled"

        # Extract the main content (adjust selectors based on typical page structure)
        # Common areas to look at: main headings, paragraphs, meta descriptions
        main_content = []
        for p in soup.select("p"):
            if len(p.get_text(strip=True)) > 50:  # Skip very short paragraphs
                main_content.append(p.get_text(strip=True))

        return title, main_content
    except requests.RequestException as e:
        # DEBUG: generate_website_content_post loops over candidate URLs and drops each one that
        # yields nothing, so this fires per rejected candidate as ordinary loop bookkeeping.
        log_debug(f"Error fetching page content: {e}")
        return None, None


def save_content_plan(user_id: int, daily_plan: list[dict]):
    """Save the planned content schedule to the database."""
    # Insert the 'daily_plan' into a database table for future reference and scheduling
    for plan in daily_plan:
        # Convert plan['post_type'] to Post Type object
        post_type = PostType[plan['post_type'].upper()]
        insert_planned_post(user_id, plan['scheduled_datetime'], post_type, plan['stage'],
                            content_mix=plan.get('content_mix'))


# A full buffer is a handful of posts, but a video post can spend minutes in Runway, so give the
# top-up lock a generous ceiling. It only bounds crash recovery — the happy path releases it.
_BUFFER_LOCK_TTL_SECONDS = 3600


def _content_buffer_settings(prefs: dict) -> Tuple[int, int]:
    """Per-user rolling-buffer window and ceiling, clamped to the supported range (issue #544)."""
    prefs = prefs or {}
    days = prefs.get("content_buffer_days") or DEFAULT_CONTENT_BUFFER_DAYS
    max_posts = prefs.get("content_buffer_max_posts") or DEFAULT_CONTENT_BUFFER_MAX_POSTS
    days = max(1, min(MAX_CONTENT_BUFFER_DAYS, int(days)))
    max_posts = max(1, min(MAX_CONTENT_BUFFER_POSTS, int(max_posts)))
    return days, max_posts


@shared_task.task
def auto_create_weekly_content(user_id: int = None):
    """Top up each user's bounded rolling buffer of ready posts (issue #544).

    Generates the `planning` posts due within the next `content_buffer_days` days, capped at
    `content_buffer_max_posts` minus what is already ready in that window. That closes the
    mid-week gap the old calendar-week window left (a weekday run found only past slots and
    generated nothing) while keeping forward LLM/video spend bounded to the buffer size — the
    same bounded path the /create_weekly_content/ button runs, so it can't be spammed.
    """
    if user_id is not None:
        log_info(f"Creating buffer content for user id: {user_id}")
        user_ids = [user_id]
    else:
        user_ids = get_user_ids_with_planned_posts_within_buffer() or []

    for uid in user_ids:
        # Only a run the API queued for one user has a 'queued' progress record waiting on it.
        _top_up_buffer_for_user(uid, requested=user_id is not None)


def _next_planned_at(user_id: int) -> Optional[datetime]:
    """When the user's next planning slot is due — best effort, never fails the run (issue #719)."""
    try:
        return get_next_planned_post_date(user_id)
    except Exception as e:
        log_warning(f"Could not read the next planned post date for user {user_id}", exc=e,
                    user_id=user_id, task_name="auto_create_weekly_content")
        return None


def _close_out_empty_run(user_id: int, requested: bool,
                         reason: ContentGenerationEmptyReason,
                         buffer_days: Optional[int] = None,
                         ready_count: Optional[int] = None,
                         buffer_max: Optional[int] = None) -> None:
    """Finish a requested run that generated nothing, saying WHY (issues #545, #719).

    `/create_weekly_content/` publishes 'queued' at dispatch, so a top-up that returns early
    (lock lost, buffer already full, nothing planned) must still close the record out or the SPA
    polls a queued run that never reports anything. The beat run publishes nothing up front, so
    it has nothing to close. The reason rides along because "0 posts are ready to review" on its
    own reads as a broken button rather than as an answer.
    """
    if not requested:
        return
    mark_empty(user_id, reason, next_planned_at=_next_planned_at(user_id),
               buffer_days=buffer_days, ready_count=ready_count, buffer_max=buffer_max)


# The pull-forward cap counts EVERY future ready post, not the buffer's own 30-day ceiling:
# `plan_content_for_user` appends a whole month after the LAST planning row, so a laid-out plan
# routinely reaches ~60 days out. Counting over MAX_CONTENT_BUFFER_DAYS would not see the posts a
# pull-forward click just generated past day 30, and each further click would re-earn the full cap
# further down the calendar — the exact runaway the cap exists to stop.
_PULL_FORWARD_READY_HORIZON_DAYS = 365


def _pull_forward_planned_posts(user_id: int, buffer_days: int,
                                buffer_max: int) -> Tuple[list[dict], int]:
    """The next planned posts BEYOND the buffer window, plus how many are already ready ahead.

    An explicit Generate click on a user whose near-term slots were all consumed (posted, or
    rejected — rejected slots are never re-planned) used to no-op forever, because the plan only
    ever appends after the last planning row and the top-up only looks `buffer_days` ahead
    (issue #719). Pulling the next slots forward is bounded by the SAME ceiling, counted across
    everything already generated ahead rather than the buffer window — otherwise repeated clicks
    would walk the buffer cap down the calendar and generate the entire plan.
    """
    ready_ahead = count_ready_posts_within_buffer(user_id, _PULL_FORWARD_READY_HORIZON_DAYS)
    limit = buffer_max - ready_ahead
    if limit <= 0:
        return [], ready_ahead
    return get_next_planned_posts_after_buffer(user_id, buffer_days, limit) or [], ready_ahead


def _top_up_buffer_for_user(user_id: int, requested: bool = False) -> None:
    """Generate this user's buffer delta under a single-flight lock.

    The count → select → generate sequence is read-then-write: overlapping runs for the same user
    (repeated /create_weekly_content/ clicks, the 01:30 beat landing on a retrying worker) would
    each read the same `already_ready` and each generate the full delta, overshooting the buffer
    and doubling LLM/video spend. The lock serialises them; the loser skips (its own next run
    re-reads the counts). Fails OPEN when Redis is down — same trade-off as the Selenium tasks.
    """
    lock_name = f"content_buffer_topup:{user_id}"
    lock_token = acquire_run_lock(lock_name, ttl_seconds=_BUFFER_LOCK_TTL_SECONDS)
    if lock_token is None:
        log_info(f"user_id {user_id}: another buffer top-up is in progress — skipping this cycle.")
        _close_out_empty_run(user_id, requested, ContentGenerationEmptyReason.ALREADY_RUNNING)
        return

    try:
        prefs = get_user_preferences(user_id) or {}
        buffer_days, buffer_max = _content_buffer_settings(prefs)
        already_ready = count_ready_posts_within_buffer(user_id, buffer_days)
        if already_ready >= buffer_max:
            log_info(f"user_id {user_id}: buffer already full ({already_ready}/{buffer_max} ready "
                    f"within {buffer_days} days). Skipping content creation.")
            _close_out_empty_run(user_id, requested, ContentGenerationEmptyReason.BUFFER_FULL,
                                 buffer_days=buffer_days, ready_count=already_ready,
                                 buffer_max=buffer_max)
            return

        planned_posts = get_planned_posts_within_buffer(user_id, buffer_days, buffer_max, already_ready)
        if not planned_posts and requested:
            # A user who clicked Generate asked for something to happen — so look past the window
            # rather than no-opping on an empty one (issue #719). The beat keeps its narrow window:
            # generating a week early on autopilot is spend nobody asked for.
            planned_posts, ready_ahead = _pull_forward_planned_posts(user_id, buffer_days, buffer_max)
            if planned_posts:
                log_info(f"user_id {user_id}: no planned posts within {buffer_days} days — pulling "
                         f"{len(planned_posts)} upcoming post(s) forward on explicit request",
                         user_id=user_id, task_name="auto_create_weekly_content")
            elif ready_ahead >= buffer_max:
                log_info(f"user_id {user_id}: {ready_ahead} post(s) already generated ahead "
                        f"(cap {buffer_max}). Skipping content creation.")
                _close_out_empty_run(user_id, requested, ContentGenerationEmptyReason.BUFFER_FULL,
                                     buffer_days=buffer_days, ready_count=ready_ahead,
                                     buffer_max=buffer_max)
                return
        if not planned_posts:
            log_info(f"user_id {user_id}: no planned posts within {buffer_days} days. "
                    f"Skipping content creation.")
            _close_out_empty_run(user_id, requested, ContentGenerationEmptyReason.NO_PLANNED_SLOTS,
                                 buffer_days=buffer_days)
            return

        log_info(f"user_id {user_id}: generating {len(planned_posts)} post(s) to top buffer up to "
                f"{buffer_max} ready within {buffer_days} days ({already_ready} already ready)")
        # Progress for the SPA (issue #545) — published for the beat run too, so a user who never
        # clicked Generate still sees why fresh drafts appeared.
        mark_in_progress(user_id, [int(post['id']) for post in planned_posts])
        # Counted locally rather than read back from Redis so the completion email still goes out
        # when the (fail-open) progress store is unavailable.
        generated = 0
        failed = 0
        try:
            for post in planned_posts:
                if _create_content_for_planned_post(post, prefs):
                    generated += 1
                else:
                    failed += 1
        finally:
            # Always close the run out: an unexpected raise here would otherwise strand the SPA
            # on 'in_progress' until the key's TTL expires.
            mark_finished(user_id)
        if generated:
            notify_content_generation_ready(user_id, generated, failed)
    finally:
        release_run_lock(lock_name, lock_token)


def _slot_weekday(user_id: int, scheduled_time) -> Optional[int]:
    """The LOCAL weekday of a stored (naive UTC) slot — the day-type calendar's key (issue #621).

    Returns None when the row has no scheduled time or the user's zone can't be resolved, in which
    case generation simply falls back to the full archetype menu.
    """
    if not isinstance(scheduled_time, datetime):
        return None
    try:
        user_tz = pytz.timezone(get_user_timezone(user_id))
        return pytz.utc.localize(scheduled_time.replace(tzinfo=None)).astimezone(user_tz).weekday()
    except Exception as e:
        log_warning(f"Could not resolve the local weekday for user {user_id} — "
                    f"generating without a day type", exc=e, user_id=user_id)
        return None


def _create_content_for_planned_post(post: dict, prefs: dict) -> bool:
    """Generate, store and status one planning post. Never raises — a bad post is skipped.

    Returns True when the post now has content, False when it could not be generated.
    """
    user_id = post['user_id']
    post_id = post['id']
    post_type = post['post_type']
    stage = post['buyer_stage']
    content_mix = post.get('content_mix')
    day_weekday = _slot_weekday(user_id, post.get('scheduled_time'))

    try:
        content, video_url = create_content(user_id, post_type, stage, post_id=post_id,
                                            content_mix=content_mix, day_weekday=day_weekday)
    except Exception as e:
        # ERROR, symmetric with the storing-half handler at the bottom of this function, which has
        # always been log_error: both end the same way — record_post_failed, and the user's planned
        # slot produces nothing.
        log_error("Skipping post: content generation failed", exc=e, user_id=user_id,
                  post_id=post_id, task_name="auto_create_weekly_content")
        record_post_failed(user_id, post_id)
        return False

    if content is None:
        # ERROR with no exc=: there is no exception to file, but the user still lost the post.
        log_error("Skipping post: content generation returned nothing", user_id=user_id,
                  post_id=post_id, task_name="auto_create_weekly_content")
        record_post_failed(user_id, post_id)
        return False

    # Everything past generation (video download, dwell scoring, the DB writes) can raise too.
    # Contain it here so one bad post is counted as failed and skipped instead of aborting the
    # whole run — which would leave the remaining posts ungenerated and the progress record stuck.
    try:
        # Copy the video from url to our assets/video folder and store it to the database for later retrieval via api call
        ai_video = False
        if video_url:
            # AI (Runway) output is a remote http URL; Pexels fallback is a local path.
            ai_video = str(video_url).startswith("http")
            # Define and create assets_dir / videos
            videos_dir = os.path.join(assets_dir, 'videos', 'runwayml')
            create_folder_if_not_exists(videos_dir)
            video_file_path = save_video_url_to_dir(video_url, videos_dir)
            log_info(f"Video from url: {video_url} | Saved to: {video_file_path}")
            # Probe before this file becomes posts.video_url (issue #1280). This is where a video
            # post is BORN, so an unprobed zero-byte download here is the one that actually reaches
            # LinkedIn — the regenerate/backfill paths only ever heal it afterwards.
            if not _accept_probed_video(post_id, video_file_path, video_url, user_id=user_id,
                                        task_name="auto_create_weekly_content"):
                # Advisory failure: keep the caption but store no media, so this is indistinguishable
                # from a failed render — `_post_missing_required_asset` holds the post PENDING with a
                # missing_asset finding and asset backfill can retry. `video_url` is cleared for that
                # reason: the gate below reads it, and a source URL whose FILE was rejected would let
                # a media-less post auto-approve. ai_video goes False too — an AI-visuals disclosure
                # for visuals the post does not have is a false claim.
                video_file_path = None
                video_url = None
                ai_video = False
            # Muted-autoplay captions (issue #1278) run on the probed file, before signing: the
            # burn re-encodes the video and would strip C2PA credentials attached first.
            if video_file_path:
                _caption_video_asset(post_id, video_file_path, content, user_id=user_id,
                                     task_name="auto_create_weekly_content")
            # Attach AI Content Credentials to AI-generated video only (not stock).
            if ai_video:
                try:
                    from cqc_lem.utilities.c2pa_helper import add_ai_content_credentials
                    add_ai_content_credentials(video_file_path)
                except Exception as e:
                    # Same as _store_video_asset: c2pa_helper never raises by contract, so this
                    # handler only sees the best-effort module itself failing.
                    log_warning("C2PA signing raised — the video ships without content credentials",
                                exc=e, user_id=user_id, post_id=post_id,
                                task_name="auto_create_weekly_content")
            if video_file_path:
                # Same as _store_video_asset: measured last, on the bytes that ship, because
                # purge_post_assets deletes this file at publish (issue #1517).
                _record_video_asset_measures(post_id, video_file_path, user_id=user_id,
                                             task_name="auto_create_weekly_content")
                # Get the file name from the video file path
                video_file_name = os.path.basename(video_file_path)

                # The video url is our api prefix + 'assets?file=videos/runwayml' +  video_file_name
                api_video_url = (f"{API_URL_FINAL}/api/assets?file_name=videos/runwayml/"
                                 f"{video_file_name}")
                log_info(f"Video URL: {api_video_url}")

                # Update the database with the video url
                update_db_post_video_url(post_id, api_video_url)

        # Disclose AI-generated visuals in the caption (caption-line fallback for C2PA).
        # A synthetic likeness of a real person needs the disclosure at least as much as a
        # stock-motion video does, so ANY media that came out of the avatar LoRA counts —
        # carousel slides and post images included, not just video (issue #744).
        if ai_video or _post_used_avatar_media(post_id):
            content = _apply_ai_disclosure(content)

        # Dwell-proxy score (issue #391 — C2) for EVERY post type: the text post, the carousel
        # caption, and the video caption all live or die on holding the reader past the fold.
        # Deterministic and advisory — recorded next to the authenticity score, never a gate.
        _score_and_persist_dwell(user_id, post_id, content)

        # Update the database with the created content
        log_info(f"Updating content for post_id: {post_id}")
        update_db_post_content(post_id, content)

        # Respect the user's auto_schedule_posts preference (fetched once per user by the caller):
        # True → APPROVED (Celery will pick it up); False → PENDING (manual review required)
        auto_schedule = bool((prefs or {}).get("auto_schedule_posts", True))
        new_status = PostStatus.APPROVED if auto_schedule else PostStatus.PENDING

        # Quality gates (issues #382 / #421): a video/carousel post whose media failed to generate,
        # or a draft the authenticity judge scored below the user's minimum, is held PENDING for
        # human review instead of auto-approved — and the REASON is recorded on the post so the
        # review queue can explain the hold and what to do about it. Only downgrades an APPROVED;
        # never upgrades a PENDING, never blocks when unscored.
        gate_findings = _gate_findings_for_post(user_id, post_id, content, post_type, video_url)
        held_by = demoting_findings(gate_findings)
        if held_by and new_status == PostStatus.APPROVED:
            new_status = PostStatus.PENDING
            log_warning(f"post_id {post_id}: held PENDING for review — "
                        f"{'; '.join(f['explanation'] for f in held_by)}",
                        user_id=user_id, post_id=post_id, task_name="create_content")
        _persist_gate_findings(user_id, post_id, gate_findings)

        log_info(f"Updating post_id: {post_id} Status={new_status}")
        update_db_post_status(post_id, new_status)
    except Exception as e:
        log_error(f"Skipping post_id {post_id}: storing generated content failed", exc=e,
                  user_id=user_id, post_id=post_id, task_name="auto_create_weekly_content")
        record_post_failed(user_id, post_id)
        return False

    record_post_generated(user_id, post_id)
    return True


def is_blog_post(url):
    """Cheap offline guess from the URL shape alone: an article, or a section/landing page?

    True on a digit-bearing path segment (a date or a post id) or on more than one segment. Costs
    nothing, which is why `is_blog_post_combined` asks it first and only pays for a page fetch on
    the URLs it rejects.
    """
    parsed = urlparse(url)
    path = parsed.path.strip('/')

    # Check if the path contains typical blog indicators
    if any(segment.isdigit() for segment in path.split('/')) or len(path.split('/')) > 1:
        return True
    return False


def is_blog_post_by_metadata(url):
    """Second opinion for a URL whose shape said nothing: does the page declare a `meta` author?

    Costs a full page fetch, so it runs only after `is_blog_post` has said no. Any fetch or parse
    failure reads as False — an unreadable page is not evidence of an article, and the cost of
    that false negative is one candidate URL dropped from the source material.
    """
    try:
        content = fetch_content(url)
        # myprint(f"Fetched URL: {url}: Content: {content}")
        soup = BeautifulSoup(content, 'html.parser')

        # Look for common blog-related tags
        # if soup.find('article') or soup.find('meta', {'name': 'author'}):
        if soup.find('meta', {'name': 'author'}):
            return True
    except Exception as e:
        # DEBUG: the docstring calls this out as expected — "an unreadable page is not evidence of
        # an article" — and filter_relevant_urls runs it over every URL in a user's sitemap.
        log_debug(f"Error fetching URL: {e}")
    return False


def is_blog_post_combined(url):
    """Is this URL worth scraping for post source material? Shape first, metadata only if needed.

    `filter_relevant_urls` runs this over every URL in a user's sitemap, so the ordering is the
    point: the free URL test settles most candidates and only what it rejects is paid for with a
    fetch.
    """
    if is_blog_post(url):
        return True
    if is_blog_post_by_metadata(url):
        return True
    return False


if __name__ == '__main__':
    log_info("Generating content plan for 30 days")
    auto_generate_content()
    log_info("Creating weekly content")
    auto_create_weekly_content()
    log_info("Process finished")
