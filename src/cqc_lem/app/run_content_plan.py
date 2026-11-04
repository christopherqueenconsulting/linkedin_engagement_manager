Warning: truncated output (original token count: 83400)
Total output lines: 6147

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
import copy
import dataclasses
import json
import os
import random
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Optional, Tuple, Union
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
from cqc_lem.utilities.ai import audience_mix as _audience_mix, story_bank as _story_bank
from cqc_lem.utilities.ai.ai_helper import (
    apply_post_guidance,
    create_runway_video,
    forget_supplied_material,
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
    record_supplied_material,
    supplied_material_for,
)
from cqc_lem.utilities.ai.content_alignment import (
    PROMO_ARCHETYPE,
    ContentMix,
    artifact_cta_line,
    assign_content_mix,
    authenticity_gate_enabled,
    authenticity_score_min,
    contains_meeting_ask,
    ensure_lead_magnet_cta,
    has_artifact_cta,
    has_lead_magnet_cta_mechanic,
    humanize_text,
    lead_magnet_cta_directive,
    lead_magnet_enabled,
    meeting_ask_excerpts,
    normalize_content_mix,
    profile_topic_dna,
    replace_meeting_ask_cta,
    score_authenticity,
    should_include_lead_magnet_cta,
    topic_authority_min,
    topic_authority_score,
)
from cqc_lem.utilities.ai.content_framework import (
    ARTIFACT_CTA_COOLDOWN_DAYS,
    CTA_TYPE_ARTIFACT,
    CTA_TYPE_NONE,
    CTA_TYPE_WINDOW,
    HOOK_SHAPE_QUESTION,
    TOPIC_CLUSTER_WINDOW,
    TOPIC_DIVERSITY_WINDOW,
    allowed_hook_shapes,
    assign_cta_style,
    capped_topic_cluster,
    cluster_avoidance_directive,
    crowded_topic_clusters,
    cta_type_of,
    day_type_for_weekday,
    day_type_formats,
    day_type_stage,
    deck_count_claims,
    deck_count_report,
    deck_figure_gate_issues,
    deck_slides,
    deck_text,
    deck_topic_report,
    dedupe_repeated_lines,
    document_deck_wording,
    document_wording,
    drop_opening_question,
    dwell_report,
    dwell_score_min,
    enforce_close_caps,
    fact_anchored_formats,
    fact_grounding_report,
    finalize_deck_claims,
    has_first_person_proof,
    history_avoidance_directive,
    hook_shape_violation,
    hook_style_for_shape,
    numeric_claims,
    occasion_stage,
    opening_shape,
    post_similarity_report,
    post_topic,
    question_hooks_capped,
    reconcile_deck_counts,
    repeated_cta_phrase,
    repeated_topic,
    requires_fact_anchor,
    select_blueprint,
    select_cta_type,
    select_hook_shape,
    shape_for_dwell,
    smb_audience,
    strip_closing_ask,
    strip_verbal_tics,
    topic_avoidance_directive,
    topic_cluster,
    weekly_post_slots,
    what_if_banned,
)
from cqc_lem.utilities.ai.fact_consistency import (
    consistency_report,
    fix_weekday_mismatches,
    strip_signature_lines,
)
from cqc_lem.utilities.ai.slop_lint import (
    CHECK_FOLD_JARGON,
    SEVERITY_HARD,
    lint_report as slop_lint_report,
    violation_reasons,
)
from cqc_lem.utilities.ai_spend_cap import generation_paused as ai_generation_paused
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
    PostApprover,
    PostStatus,
    PostType,
    count_ready_posts_within_buffer,
    delete_planned_posts,
    get_active_user_ids,
    get_draftable_curated_sources,
    get_engagement_preferences,
    get_future_post_slots,
    get_last_planned_post_date_for_user,
    get_lead_magnet_settings,
    get_newsletter_settings,
    get_next_planned_post_date,
    get_next_planned_posts_after_buffer,
    get_planned_posts_within_buffer,
    get_post_authenticity_score,
    get_post_content,
    get_post_content_mix,
    get_post_ever_gate_demoted,
    get_post_gate_reason,
    get_post_texts_near_slot,
    get_post_type_counts,
    get_profile_synthesis,
    get_recent_content_mix_sequence,
    get_recent_post_records,
    get_recent_post_shape_history,
    get_recent_post_texts,
    get_recent_post_topics,
    get_shape_performance,
    get_story_bank_entries,
    get_user_blog_url,
    get_user_ids_with_planned_posts_within_buffer,
    get_user_password_pair_by_id,
    get_user_preferences,
    get_user_sitemap_url,
    get_user_timezone,
    insert_planned_post,
    mark_post_gate_demoted,
    normalize_posting_days,
    post_is_curated,
    record_story_bank_use,
    update_db_post_authenticity_score,
    update_db_post_carousel_slides,
    update_db_post_content,
    update_db_post_dwell_score,
    update_db_post_gate_reason,
    update_db_post_shape,
    update_db_post_status,
    update_db_post_video_url,
    update_post_content_mix,
    update_post_generation_record,
)
from cqc_lem.utilities.engagement_mode import browser_automation_allowed
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
from cqc_lem.utilities.linkedin_formatter import (
    normalize_currency_symbols,
    sanitize_for_linkedin,
    strip_engagement_bait,
)
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning
from cqc_lem.utilities.media_provenance import write_brief_receipt
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
    GATE_DECK_COUNT,
    GATE_DECK_TOPIC,
    GATE_FACT_CONSISTENCY,
    GATE_FACT_GROUNDING,
    GATE_FORBIDDEN_CLAIM,
    GATE_MALFORMED_ASSET,
    GATE_SIMILARITY,
    GATE_SLIDE_SLOP,
    affiliate_promo_finding,
    authenticity_finding,
    deck_count_finding,
    deck_topic_finding,
    demoting_findings,
    fabrication_finding,
    fact_consistency_finding,
    fact_grounding_finding,
    focus_finding,
    forbidden_claim_finding,
    malformed_asset_finding,
    meeting_cta_finding,
    missing_asset_finding,
    parse_gate_findings,
    promo_artifact_cta_finding,
    proof_finding,
    similarity_finding,
    slide_slop_finding,
    slop_finding,
)
from cqc_lem.utilities.selenium_util import get_driver_wait_pair, quit_gracefully
from cqc_lem.utilities.trial_access import trial_lapsed
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


def _resolve_cadence(user_id: int) -> tuple:
    """`(weekdays, posts_per_week)` for this user — the ONE reading of their cadence.

    Extracted so the plan walk and the tail reconcile (issue #2021) cannot disagree about what the
    cadence currently is. They would otherwise each resolve it, and a reconcile that trimmed the
    tail to a different answer than the planner refills it with is worse than no reconcile at all.

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
    elif len(posting_days) > posts_per_week:
        # DEBUG, not a warning: the shipped default is 3/week over Mon-Fri, so this is the ordinary
        # case rather than a defect. It is logged at all because the days a user switched on and
        # never sees a post on is what issue #1526 was reported as — the SPA's C31 finding is the
        # user-facing half of the same fact.
        log_debug(f"Cadence fills {posts_per_week} of the {len(posting_days)} day(s) switched on "
                  f"for user {user_id}: {sorted(weekdays)}")
    return weekdays, posts_per_week


def _cadence_slots(user_id: int, start_date: datetime, end_date: datetime) -> list:
    """The dates this user's day-type calendar fills between two dates, inclusive."""
    weekdays, _posts_per_week = _resolve_cadence(user_id)
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


def _user_zone(user_id: int):
    """The user's tz, or UTC when it cannot be resolved — the same fallback `_schedule_slot_utc` has."""
    try:
        return pytz.timezone(get_user_timezone(user_id))
    except Exception:
        return pytz.utc


def _local(when: datetime, tz) -> datetime:
    """A stored naive-UTC instant as the user's wall clock. `tz=None` reads it as already local.

    The cadence is a LOCAL calendar — a Saturday slot is Saturday where the user lives — so every
    weekday and ISO-week judgement is made on this, never on the stored UTC instant (issue #2137).
    """
    if tz is None:
        return when
    return pytz.utc.localize(when).astimezone(tz).replace(tzinfo=None)


def planned_slots_to_drop(rows: list, weekdays: set, posts_per_week: int, tz=None) -> list:
    """Which future PLANNING post ids the current cadence would not have laid (issue #2021).

    Pure, so the rule is testable without a database. Two reasons a slot goes:

    * its weekday is not in `posting_days` — the HARDER bound (#581): cadence says how many,
      the allow-list says which days are eligible at all;
    * its ISO week already holds `posts_per_week` slots. The EARLIEST slots in a week are kept, so
      lowering a cadence trims from the end of each week rather than reshuffling what is already
      laid — the same "adding days never moves the ones in use" property `weekly_post_slots` has.

    A row is `(id, scheduled_time)` or `(id, scheduled_time, status)`. A row past `planning` is a
    commitment: it is never dropped, but it does fill its week first (issue #2137), so an approved
    post leaves one fewer planning slot in that week rather than one more post than the cadence.

    Returns ids only. Deciding is separate from deleting on purpose: this function cannot touch a
    row that somebody approved, because it never touches a row at all.
    """
    cap = max(0, int(posts_per_week))
    per_week: dict = {}
    planned = []
    for row in rows or []:
        when = row[1]
        if when is None:
            continue
        local = _local(when, tz)
        if len(row) > 2 and row[2] != PostStatus.PLANNING.value:
            key = local.isocalendar()[:2]
            per_week[key] = per_week.get(key, 0) + 1
        else:
            planned.append((row[0], local))
    drop = []
    for post_id, local in planned:
        if weekdays and local.weekday() not in weekdays:
            drop.append(post_id)
            continue
        key = local.isocalendar()[:2]
        per_week[key] = per_week.get(key, 0) + 1
        if per_week[key] > cap:
            drop.append(post_id)
    return drop


def reconcile_planned_cadence(user_id: int, weekdays: set, posts_per_week: int,
                              tz=None) -> Optional[list]:
    """Drop the future planning slots the user's CURRENT cadence would not have laid.

    Only `planning`, only the future, and the repository re-asserts both in its own WHERE clause —
    an approved, scheduled or published post is a commitment and is never in scope. A planning row
    is a slot reservation carrying a placeholder body, so dropping one costs the slot and nothing
    else.

    Returns the future rows that survive, for the refill to read — or `None` when the tail could not
    be read or not every drop landed. The refill fails CLOSED on `None`: a slot it cannot see is not
    a hole, and treating it as one would put two posts on one day.
    """
    rows = get_future_post_slots(user_id)
    if rows is None:
        return None
    drop = planned_slots_to_drop(rows, weekdays, posts_per_week, tz)
    if not drop:
        return rows
    dropped = delete_planned_posts(user_id, drop)
    log_info(f"Content Plan | dropped {dropped} planned slot(s) the current cadence would not "
             f"have laid", user_id=user_id, task_name="auto_create_content_plan")
    if dropped != len(drop):
        return None
    gone = set(drop)
    return [row for row in rows if row[0] not in gone]


def cadence_holes(candidates: list, occupied: list, posts_per_week: int) -> list:
    """The cadence dates a laid tail is missing, week by week (issue #2137).

    Pure. `candidates` are the local dates the cadence calendar owns (already inside
    `posting_days`); `occupied` the local dates already carrying a future post of any status. A
    candidate is a hole when its date is free and its ISO week holds fewer than `posts_per_week`
    posts — counting the committed ones, so a refill tops a week UP to the cadence and never past it.

    Without this the reconcile could only take slots away: it trimmed the production tail to two a
    week, and the planner — which only ever extends the plan past its LAST row — was held off by that
    same tail reaching 30+ days out, so the weeks it thinned were never topped up.
    """
    taken = set(occupied or [])
    per_week: dict = {}
    for day in taken:
        key = day.isocalendar()[:2]
        per_week[key] = per_week.get(key, 0) + 1
    holes = []
    for day in sorted(set(candidates or [])):
        key = day.isocalendar()[:2]
        if day in taken or per_week.get(key, 0) >= posts_per_week:
            continue
        per_week[key] = per_week.get(key, 0) + 1
        holes.append(day)
    return holes


def _refill_slots(user_id: int, weekdays: set, posts_per_week: int, tz, rows: Optional[list],
                  last_planned_date: Optional[datetime]) -> list:
    """The holes in the laid tail between tomorrow and the tail's end, at most 30 days out.

    Bounded by the tail's last row on purpose: past it, the forward plan lays the calendar as it
    always has, and two passes over the same dates would double-book them.
    """
    if rows is None or not last_planned_date or last_planned_date <= datetime.now():
        return []
    today = datetime.now(tz).date()
    last = min(_local(last_planned_date, tz).date(), today + timedelta(days=30))
    candidates = []
    day = today + timedelta(days=1)
    while day <= last:
        if day.weekday() in weekdays:
            candidates.append(day)
        day += timedelta(days=1)
    occupied = [_local(row[1], tz).date() for row in rows if row[1] is not None]
    holes = cadence_holes(candidates, occupied, posts_per_week)
    if holes:
        log_info(f"Content Plan | refilling {len(holes)} cadence slot(s) inside the laid tail",
                 user_id=user_id, task_name="auto_create_content_plan")
    return holes


def _forward_slots(user_id: int, last_planned_date: Optional[datetime]) -> list:
    """The slots past the laid tail: the day after its last row through the end of that month."""
    # Use the last planned date if it exists and it is after today
    if last_planned_date and last_planned_date.date() > datetime.now().date():
        start_date = last_planned_date + timedelta(days=1)  # Start with the next day

        # If the start date is greate than 30 days from today just skip the process
        if start_date > datetime.now() + timedelta(days=30):
            log_info(f"Content Plan | Start Date: {start_date} | >30 days out | Skipped")
            return []
    else:
        start_date = datetime.now() + timedelta(days=1)  # Start with the next day
    log_info(f"Content Plan | Start Date: {start_date}")

    # The plan still runs to the end of the month, but the SLOTS in that window come from the
    # user's day-type calendar (issue #621) instead of one post on every remaining day.
    end_of_month = _plan_window_end(start_date)
    slots = _cadence_slots(user_id, start_date, end_of_month)
    if not slots:
        # The tail of a month can land on no cadence weekday at all (e.g. a Tue/Wed/Thu calendar
        # starting the last Friday) — stopping here stalled the plan at the boundary until a LATER
        # day's run happened to compute a start date already inside next month, silently starving
        # the buffer for days around every month-end (issue #1725). Roll the window one month
        # further once instead of skipping, so the plan always advances.
        next_window_end = _plan_window_end(end_of_month + timedelta(days=1))
        slots = _cadence_slots(user_id, start_date, next_window_end)
    if not slots:
        log_info(f"Content Plan | No cadence slots left this month after {start_date} | Skipped")
    return slots


def _time_slots(slots: list, user_id: int, weekdays: set, tz, occupied: list) -> list:
    """`[(post_date, scheduled_utc)]` for the slots that can be laid without breaking the cadence.

    Each slot is held 24h after the latest post before it — laid or already stored — and a slot is
    NOT laid (issue #2137) when that hold pushes it onto a weekday outside `posting_days`, or when it
    lands inside 24h of any stored post. The floor only sees posts on EARLIER local days, so that
    check is both ways: a refilled hole can sit just before a stored post, and east of UTC the
    forward plan's first slot can share a local day with the tail's last post — which the floor,
    seeded with that post until #2137, used to push off.
    """
    stored = sorted(o for o in occupied if o is not None)
    laid: list = []
    timed = []
    for post_date in sorted(slots):
        before = [o for o in stored if _local(o, tz).date() < post_date] + laid
        previous_utc = max(before) if before else None
        scheduled = _schedule_slot_utc(post_date, user_id, previous_utc)
        if weekdays and _local(scheduled, tz).weekday() not in weekdays:
            log_debug(f"Content Plan | {post_date} slot pushed off the posting days by the 24h "
                      f"floor — not laid", user_id=user_id)
            continue
        if any(abs(o - scheduled) < MIN_POST_INTERVAL for o in stored):
            log_debug(f"Content Plan | {post_date} slot sits inside 24h of a stored post — not laid",
                      user_id=user_id)
            continue
        laid.append(scheduled)
        timed.append((post_date, scheduled))
    return timed


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

    # Reconcile the tail BEFORE reading it (issue #2021). A cadence change only affects posts the
    # planner has not laid yet, and the planner will not lay any while the tail already reaches past
    # its horizon — so without this, a change to `posts_per_week` or `posting_days` does not take
    # effect until the existing tail drains. Production on 2026-09-10: the tail ran to 2026-10-30, so
    # a change made in September would first apply in November, and the account kept publishing
    # 5/week against a setting of 3 for seven weeks. A setting that takes seven weeks to do anything
    # reads to the user as a setting that does nothing.
    weekdays, per_week = _resolve_cadence(user_id)
    user_tz = _user_zone(user_id)
    rows = reconcile_planned_cadence(user_id, weekdays, per_week, user_tz)

    # Determine the start date as the day after the last scheduled post in planning status
    last_planned_date = get_last_planned_post_date_for_user(user_id)

    # Trimming alone left production at 2/week for a fortnight (issue #2137): the reconcile dropped
    # Mon/Wed/Fri off a Mon-Fri tail, and the forward plan never runs while that tail reaches 30+
    # days out, so the Saturday the new `posting_days` owns was never laid. Refill the holes inside
    # the tail too.
    slots = sorted(set(_refill_slots(user_id, weekdays, per_week, user_tz, rows, last_planned_date))
                   | set(_forward_slots(user_id, last_planned_date)))
    if not slots:
        return

    # The 24h floor also has to hold ACROSS planning runs. `slots` only knows about this run, so a
    # plan that starts the day after the previous plan's last post — a 7/week calendar rolling into
    # a new month, or a user raising their cadence mid-month — would otherwise stack two posts
    # inside one 24h window. Seed the floor with every post already stored ahead of us; when that
    # read failed, with the user's last one, which is the stored naive-UTC instant the floor is
    # measured on.
    if rows is not None:
        occupied = [row[1] for row in rows]
    elif isinstance(last_planned_date, datetime) and last_planned_date > datetime.now():
        occupied = [last_planned_date]
    else:
        occupied = []
    timed = _time_slots(slots, user_id, weekdays, user_tz, occupied)
    if not timed:
        log_info("Content Plan | No slot survived the posting-day and 24h bounds | Skipped",
                 user_id=user_id, task_name="auto_create_content_plan")
        return

    target_posts = len(timed)
    log_info(f"Content Plan | {target_posts} slot(s) through {timed[-1][0]} "
             f"on weekdays {sorted({d.weekday() for d, _ in timed})}")

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
    # Classed against the posts the user already has (issue #2107), and the promo waits for a day
    # whose type draws a case study.
    history = get_recent_content_mix_sequence(user_id)
    promo_slots = _promo_slots(timed, weekdays)
    if history is None:
        # The read already logged its error. Without the history the governor cannot tell whether
        # a promo is due, so this plan lays none: the ceiling is the harder bound.
        promo_slots = [False] * target_posts
    mix_classes = assign_content_mix(target_posts, history=history, promo_slots=promo_slots)

    for day, (post_date, scheduled_datetime) in enumerate(timed):
        content_mix = mix_classes[day]

        # Choose a post type from the shuffled list
        post_type = _take_planned_post_type(post_types, content_mix)

        stage = day_type_stage(post_date.weekday())

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


def _promo_slots(timed: list, weekdays: set) -> Optional[list]:
    """Which planned slots may carry the promo post (issue #2107).

    Those on a weekday whose day type draws a case study, since the promo is forced into one. None
    when the user's calendar owns no such day, so the promo still lands somewhere.
    """
    fits = {wd for wd in (weekdays or ()) if PROMO_ARCHETYPE in day_type_formats(wd)}
    if not fits:
        return None
    return [post_date.weekday() in fits for post_date, _ in timed]


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
                   content_mix: str = None, day_weekday: int = None,
                   brief_info: dict = None):
    """Create content based on the specified post type and buyer journey stage.

    `content_mix` is the post's 70/20/10 class from the plan governor (issue #618) — it steers the
    text-post prompt.

    `day_weekday` is the slot's LOCAL weekday (Mon=0 … Sun=6); it selects the day-type calendar's
    archetype family (issue #621) so a Wednesday reads as a story and a Thursday as a spiky POV.
    Text posts and carousels draw from the same post menu, so both honor it (issue #2107). Videos
    carry their own template menus and are unaffected.

    `brief_info` is the VIDEO path's out-param only (issue #1377): a text post's image is stored by
    `generate_image_for_post`, which records its own brief, but a video's file is downloaded and
    stored by the caller, so the brief has to travel there.
    """
    video_url = None
    content = None

    if post_type == PostType.VIDEO.value:
        content, video_url = create_video_content(user_id, stage, post_id=post_id,
                                                  brief_info=brief_info, content_mix=content_mix)
    elif post_type in (PostType.CAROUSEL.value, PostType.DOCUMENT.value):
        # A document post IS a carousel deck — same generated slides, published as a
        # native PDF instead of a multi-image share (see share_document_on_linkedin).
        content = create_carousel_content(user_id, stage, post_id, day_weekday=day_weekday,
                                          post_type=post_type)
    else:
        # Affiliate promotion (issue #770) CLAIMS this promo slot rather than adding a post beside
        # it, which is what keeps LEM promotion inside the same 10% ceiling the author's own case
        # studies live under. It writes only for a user who recorded (B) consent, on 1 in N promo
        # slots; every other slot falls through to the ordinary case-study post unchanged.
        content = _affiliate_promo_content(user_id, post_id, content_mix)
        if content is None:
            # A curated outside source (docs/curated-sources.md) claims at most one value/authority
            # slot in three. Its drafter owns the post's media — a re-chart image, the publisher's
            # own link-card thumbnail, or nothing for a reshare — so no photo is generated for it.
            curated = _curated_source_content(user_id, post_id, content_mix)
            if curated:
                return curated.strip(), video_url
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
        # Opt-in animated loop of this SAME gated still (ANIMATED_POST_ENABLED, default OFF —
        # docs/animated-posts.md). Never raises and never touches the still.
        from cqc_lem.utilities.animated_loop import produce_post_loop
        produce_post_loop(user_id, post_id, text_content, api_image_url)
        return api_image_url
    except Exception as e:
        log_warning("Could not generate the post image — the post ships without one", exc=e,
                    user_id=user_id, post_id=post_id, task_name="create_content")
        return None


def _affiliate_promo_content(user_id: int, post_id: Optional[int],
                             content_mix: Optional[str]) -> Optional[str]:
    """The LEM-promotional post for this slot, or None to write the author's own post instead.

    Never raises: the (B) writer is an opt-in extra, and a user who consented to it must not lo…63400 tokens truncated…ption for HTTP errors
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
                                  story_directive: str = None, content_mix: str = None,
                                  post_id: Optional[int] = None):
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
            # The user's own page is the source every number in this draft may come from (#1971).
            record_supplied_material(post_id, content)
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
    # A free trial that ended with no paid plan (issue #2380) stops generation outright. The beat
    # walks every user with a planned slot, not only the active ones, so this is the gate — not the
    # fan-out. DEBUG: a lapsed trial being skipped every night is the designed answer.
    if trial_lapsed(user_id):
        log_debug("Content top-up skipped: the free trial has ended", user_id=user_id,
                  task_name="auto_create_weekly_content")
        _close_out_empty_run(user_id, requested, ContentGenerationEmptyReason.TRIAL_ENDED)
        return

    # The free-trial daily AI cap (issue #2378) pauses this run until the next UTC day — checked
    # before the lock, so a paused user neither generates nor holds the lock. A user who clicked
    # Generate is told why; the beat run just resumes tomorrow.
    if ai_generation_paused(user_id):
        log_debug("Content top-up paused by the free-trial daily AI cap", user_id=user_id,
                  task_name="auto_create_weekly_content")
        _close_out_empty_run(user_id, requested, ContentGenerationEmptyReason.DAILY_AI_LIMIT)
        return

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

    # Filled by the video path with the source frame's brief, so the store below can record it
    # beside the MP4 (issue #1377). Empty for every other post type.
    brief_info: dict = {}
    try:
        content, video_url = create_content(user_id, post_type, stage, post_id=post_id,
                                            content_mix=content_mix, day_weekday=day_weekday,
                                            brief_info=brief_info)
    except Exception as e:
        # ERROR, symmetric with the storing-half handler at the bottom of this function, which has
        # always been log_error: both end the same way — record_post_failed, and the user's planned
        # slot produces nothing.
        log_error("Skipping post: content generation failed", exc=e, user_id=user_id,
                  post_id=post_id, task_name="auto_create_weekly_content",
                  terminal_state="failed", status=PostStatus.ERROR.value,
                  reason="generation_exception", gate_reasons=[])
        record_post_failed(user_id, post_id)
        return False

    if content is None:
        # ERROR with no exc=: there is no exception to file, but the user still lost the post.
        log_error("Skipping post: content generation returned nothing", user_id=user_id,
                  post_id=post_id, task_name="auto_create_weekly_content",
                  terminal_state="failed", status=PostStatus.ERROR.value,
                  reason="empty_generation_result", gate_reasons=[])
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
            try:
                video_file_path = save_video_url_to_dir(video_url, videos_dir)
            except Exception:
                # The download raises on a non-2xx / dropped connection, so this post never got the
                # file the recorded render model names — clear it (issue #1410) before the outer
                # handler turns this into a failed post. Only the download is wrapped: everything
                # after `update_db_post_video_url` below has a stored asset the key correctly
                # describes, and clearing there would erase a true record.
                _persist_video_model(post_id, None)
                raise
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

                # The source frame's brief, keyed by the URL the row is about to carry (#1377).
                # Nothing is written when the Pexels fallback ran — it cleared the key, because
                # stock footage was never what this brief described.
                write_brief_receipt(api_video_url, brief_info.get("brief"), post_id=post_id,
                                    user_id=user_id,
                                    gate_verdict=brief_info.get("gate_verdict"),
                                    extra=_clip_check_extra(brief_info))

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
        auto_schedule = bool((prefs or {}).get("auto_schedule_posts", False))

        # Quality gates (issues #382 / #421): a video/carousel post whose media failed to generate,
        # or a draft the authenticity judge scored below the user's minimum, is held PENDING for
        # human review instead of auto-approved — and the REASON is recorded on the post so the
        # review queue can explain the hold and what to do about it. Only downgrades an APPROVED;
        # never upgrades a PENDING, never blocks when unscored.
        gate_findings = _gate_findings_for_post(user_id, post_id, content, post_type, video_url)
        # The research recorded for the writer (issue #1971) has done its job once the gates ran.
        forget_supplied_material(post_id)
        held_by = demoting_findings(gate_findings)
        if held_by and auto_schedule:
            log_warning(f"post_id {post_id}: held PENDING for review — "
                        f"{'; '.join(f['explanation'] for f in held_by)}",
                        user_id=user_id, post_id=post_id, task_name="create_content")
        # The ONE approve decision (issue #1134) — also the place a REPAIRED post is held even
        # though every gate now passes. `mark_repaired` is deliberately not passed here: this is a
        # pre-existing call site and the flag is only ever raised by the repair path itself.
        new_status = (PostStatus.APPROVED
                      if _may_auto_approve(user_id, post_id, auto_schedule, gate_findings)
                      else PostStatus.PENDING)
        _persist_gate_findings(user_id, post_id, gate_findings)

        log_info(f"Updating post_id: {post_id} Status={new_status}")
        update_db_post_status(post_id, new_status, approved_by=PostApprover.AUTO_SCHEDULE)
        try:
            _log_content_plan_terminal_state(user_id, post_id, new_status, auto_schedule,
                                             gate_findings)
        except Exception as audit_error:
            log_warning("Could not record content-plan terminal state", exc=audit_error,
                        user_id=user_id, post_id=post_id)
    except Exception as e:
        log_error(f"Skipping post_id {post_id}: storing generated content failed", exc=e,
                  user_id=user_id, post_id=post_id, task_name="auto_create_weekly_content",
                  terminal_state="failed", status=PostStatus.ERROR.value,
                  reason="persistence_exception", gate_reasons=[])
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
