import calendar
import json
import os
import random
from datetime import datetime, timedelta
from typing import Optional, Tuple
from urllib.parse import urlparse
from xml.etree import ElementTree

import pytz
import requests
from bs4 import BeautifulSoup
from cqc_lem import assets_dir
from cqc_lem.app.my_celery import app as shared_task
from cqc_lem.utilities.ai.ai_helper import get_blog_summary_post_from_ai, get_website_content_post_from_ai, \
    get_flux_image_prompt_from_ai, generate_flux1_image_from_prompt, get_runway_ml_video_prompt_from_ai, \
    create_runway_video, get_ai_linked_post_refinement, optimize_post_hook
from cqc_lem.utilities.ai.ai_helper import get_thought_leadership_post_from_ai, \
    get_industry_news_post_from_ai, get_personal_story_post_from_ai, generate_engagement_prompt_post, \
    get_or_create_profile_synthesis, apply_post_guidance
from cqc_lem.utilities.db import get_post_type_counts, insert_planned_post, update_db_post_content, \
    get_last_planned_post_date_for_user, get_user_password_pair_by_id, \
    get_user_blog_url, get_user_sitemap_url, get_active_user_ids, PostStatus, \
    update_db_post_video_url, update_db_post_status, PostType, get_user_preferences, \
    update_db_post_carousel_slides, get_post_content, get_user_timezone, get_engagement_preferences
from cqc_lem.utilities.db import count_ready_posts_within_buffer, get_planned_posts_within_buffer, \
    get_user_ids_with_planned_posts_within_buffer, DEFAULT_CONTENT_BUFFER_DAYS, \
    DEFAULT_CONTENT_BUFFER_MAX_POSTS, MAX_CONTENT_BUFFER_DAYS, MAX_CONTENT_BUFFER_POSTS
from cqc_lem.utilities.db import get_recent_post_shape_history, update_db_post_shape, get_lead_magnet_settings, \
    get_shape_performance
from cqc_lem.utilities.db import get_recent_post_texts, update_db_post_authenticity_score, \
    get_post_authenticity_score, update_db_post_dwell_score
from cqc_lem.utilities.ai.content_framework import select_blueprint, history_avoidance_directive, \
    find_most_similar, post_similarity_max, has_first_person_proof, shape_for_dwell, dwell_report, \
    dwell_score_min
from cqc_lem.utilities.ai.content_alignment import (
    should_include_lead_magnet_cta, lead_magnet_cta_directive, ensure_lead_magnet_cta,
    personal_proof_directive, topic_authority_score, topic_authority_min, profile_topic_dna,
    content_matches_focus, score_authenticity, authenticity_gate_enabled, authenticity_score_min,
    humanize_text)
from cqc_lem.utilities.env_constants import API_URL_FINAL, DEFAULT_VIDEO_RATIO, \
    DEFAULT_IMAGE_RATIO, AI_DISCLOSURE_ENABLED, AI_DISCLOSURE_TEXT, \
    STANDARD_VIDEO_MODEL, PREMIUM_VIDEO_MODEL, PREMIUM_TOP_VIDEO_MODEL, \
    PREMIUM_VIDEO_CREDITS, PREMIUM_TOP_VIDEO_CREDITS
from cqc_lem.utilities.linkedin.helper import get_my_profile, load_profile_for_user
from cqc_lem.utilities.linkedin_formatter import sanitize_for_linkedin, strip_engagement_bait
from cqc_lem.utilities.linkedin.profile import LinkedInProfile
from cqc_lem.utilities.logger import myprint, log_info, log_warning
from cqc_lem.utilities.observability import attribute_llm_cost, llm_attribution, FEATURE_CONTENT
from cqc_lem.utilities.selenium_util import get_driver_wait_pair, quit_gracefully
from cqc_lem.utilities.utils import get_best_posting_time, get_post_time, create_folder_if_not_exists, save_video_url_to_dir
from requests.adapters import HTTPAdapter
from urllib3 import Retry

# Post types the 30-day plan balances across. Native documents (PDF decks) are 2026's
# highest-reach format, so they get an equal share alongside text/carousel/video.
PLANNED_POST_TYPES = [PostType.CAROUSEL.value, PostType.TEXT.value,
                      PostType.VIDEO.value, PostType.DOCUMENT.value]


@shared_task.task
def auto_generate_content():
    # Get active users from DB
    active_users = get_active_user_ids()
    # For each user generate content for the next 30 days
    for user_id in active_users:
        plan_content_for_user.apply_async(kwargs={"user_id": user_id})


@shared_task.task(bind=True, reject_on_worker_lost=True, rate_limit='1/m')
def plan_content_for_user(self, user_id: int):
    """
    Generate and plan content for the next 30 days based on current content representation in the database.
    Ensures a balanced distribution of post types (carousel, text, video) and buyer journey stages.
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
    myprint(f"Days in Month: {days_in_month}")

    # Determine the start date as the day after the last scheduled post in planning status
    last_planned_date = get_last_planned_post_date_for_user(user_id)

    # myprint(f"Last Planned Date: {last_planned_date}")

    # Use the last planned date if it exists and it is after today
    if last_planned_date and last_planned_date.date() > datetime.now().date():
        start_date = last_planned_date + timedelta(days=1)  # Start with the next day

        # If the start date is greate than 30 days from today just skip the process
        if start_date > datetime.now() + timedelta(days=30):
            myprint(f"Content Plan | Start Date: {start_date} | >30 days out | Skipped")
            return
    else:
        start_date = datetime.now() + timedelta(days=1)  # Start with the next day
    myprint(f"Content Plan | Start Date: {start_date}")



    # Determine how many days are left in this month
    days_left_in_month = days_in_month - start_date.day

    myprint(f"Days Left in Month after Start Date: {days_left_in_month}")

    # Need on more post than days left in month
    target_posts = days_left_in_month + 1  # Total posts till the end of the month

    # myprint(f"Target Posts: {target_posts}")

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

    # 3. Plan content across the buyer journey stages | logic: randomly select a post type and buyer journey stage for each post
    # Define buyer journey stages: Awareness, Consideration, Decision
    journey_stages = ["awareness", "consideration", "decision"]
    daily_plan = []

    # Shuffle the journey stages to ensure a random order
    random.shuffle(journey_stages)

    # Create a list of post types based on needed_posts
    post_types = []
    for post_type, count in needed_posts.items():
        post_types.extend([post_type] * count)

    # Shuffle the post types to ensure a random order
    random.shuffle(post_types)

    # Generate content plan evenly across buyer journey stages and post types
    for day in range(target_posts):  # Plan for end of this month
        # Choose a post type from the shuffled list
        post_type = post_types.pop()

        # Choose a buyer journey stage in a round-robin fashion
        stage = journey_stages[day % len(journey_stages)]

        # Add this post to the daily plan
        post_date = start_date + timedelta(days=day)

        # Get the best time for the selected date — the user's data-driven best hour when we have
        # enough post-stats, else the 2026 default peak model.
        post_time = get_post_time(post_date.date(), user_id)

        # get_best_posting_time returns the user's LOCAL audience time (e.g. 2pm). Convert
        # it from the user's timezone to UTC for storage, because the scheduler treats
        # stored scheduled_time as UTC. Without this, a 14:00 local post was stored as
        # 14:00 UTC and fired hours off the intended local time.
        scheduled_datetime = datetime.combine(post_date, post_time)
        try:
            user_tz = pytz.timezone(get_user_timezone(user_id))
            scheduled_datetime = (
                user_tz.localize(scheduled_datetime).astimezone(pytz.utc).replace(tzinfo=None)
            )
        except Exception as e:
            myprint(f"Timezone conversion failed for user {user_id} — storing as UTC: {e}")

        daily_plan.append({
            "scheduled_datetime": scheduled_datetime,
            "post_type": post_type,
            "stage": stage
        })

        # Update the needed_posts count for the selected post type
        needed_posts[post_type] -= 1

    # Log the final content plan for the next 30 days
    # myprint(f"Generated content plan: {daily_plan}")
    myprint(f"Generated content plan")

    # 4. Save the daily plan to the database for tracking and scheduling
    save_content_plan(user_id, daily_plan)


# Function to find the key with the highest value in a dictionary
def get_max_key(d):
    return max(d, key=d.get)


# Function to find the key with the lowest value in a dictionary
def get_min_key(d):
    return min(d, key=d.get)


def create_content(user_id: int, post_type: str, stage: str, post_id: int = None):
    """Create content based on the specified post type and buyer journey stage."""

    video_url = None
    content = None

    if post_type == PostType.VIDEO.value:
        content, video_url = create_video_content(user_id, stage, post_id=post_id)
    elif post_type in (PostType.CAROUSEL.value, PostType.DOCUMENT.value):
        # A document post IS a carousel deck — same generated slides, published as a
        # native PDF instead of a multi-image share (see share_document_on_linkedin).
        content = create_carousel_content(user_id, stage, post_id)
    else:
        content = create_text_post(user_id, stage, post_id=post_id)
        if content:
            content = content.strip()

    return content, video_url


@attribute_llm_cost(FEATURE_CONTENT)
def create_carousel_content(user_id: int, stage: str, post_id: int = None,
                            template: Optional[str] = None) -> str:
    """Generate AI carousel content, render slide images, update DB, and return the post text."""
    from cqc_lem.utilities.ai.ai_helper import generate_carousel_content
    from cqc_lem.utilities.carousel_creator import (
        create_ppt, create_carousel_slide_images,
        EducationalContentCarousel, CaseStudyCarousel, PersonalStoryCarousel,
        IndustryInsightsCarousel, EventRecapCarousel, TestimonialCarousel, ProductDemoCarousel,
    )

    # Same alignment inputs as text posts (best-effort — carousel generation never blocks on them).
    try:
        prefs = get_engagement_preferences(user_id)
    except Exception as e:
        myprint(f"Could not load engagement preferences for carousel: {e}")
        prefs = None
    try:
        profile_synthesis = get_or_create_profile_synthesis(user_id)
    except Exception as e:
        myprint(f"Could not load profile synthesis for carousel: {e}")
        profile_synthesis = None
    post_text, carousel_dict = generate_carousel_content(user_id, stage, prefs=prefs,
                                                         profile_synthesis=profile_synthesis)
    myprint(f"Carousel AI content generated for user_id={user_id} stage={stage}")

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
        myprint(f"Could not parse carousel dict into {model_cls.__name__}: {e} — using raw slide texts")

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
            myprint(f"Carousel slides saved: {len(slide_urls)} images for post_id={post_id}")

            # Also generate PPTX for user download
            try:
                ppt_name = f"carousel_{post_id}"
                create_ppt(ppt_name, carousel_obj, post_id=post_id, user_id=user_id)
            except Exception as ppt_err:
                myprint(f"PPTX generation failed (non-fatal): {ppt_err}")
        except Exception as img_err:
            myprint(f"Carousel image generation failed: {img_err}")
            # Do NOT fall back to text/placeholder images — that produced carousels of a
            # single repeated default image. Flag the post 'error' for manual/dev fix.
            if post_id is not None:
                update_db_post_status(post_id, PostStatus.ERROR)
                myprint(f"Post {post_id} flagged 'error' — carousel slide images could not be generated.")

    elif carousel_obj is None and post_id is not None:
        # Couldn't build a valid carousel model — flag for manual fix rather than degrade.
        update_db_post_status(post_id, PostStatus.ERROR)
        myprint(f"Post {post_id} flagged 'error' — could not generate a valid carousel model.")

    return post_text


def _post_missing_required_asset(post_id: int, post_type, video_url) -> bool:
    """True when a video post has no video or a carousel post has no slides — used to
    hold such posts PENDING instead of approving them to publish with no media."""
    pt = str(post_type).lower()
    if pt == PostType.VIDEO.value:
        return not video_url
    if pt in (PostType.CAROUSEL.value, PostType.DOCUMENT.value):
        from cqc_lem.utilities.db import get_post_carousel_slides
        slides = get_post_carousel_slides(post_id)
        # Missing if empty OR only text titles (no real slide images generated yet).
        return not _carousel_slides_are_real_images(slides)
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
    from cqc_lem.utilities.ai.video_models import is_premium
    from cqc_lem.utilities.ai.ai_helper import generate_post_image
    from cqc_lem.utilities.db import (get_post_video_quality, get_video_credit_balance,
                                      deduct_video_credits, refund_video_credits, get_active_avatar,
                                      get_default_video_quality)

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
            myprint(f"Premium video requested but no credits for user {user_id} — using standard")

    try:
        image_prompt = get_flux_image_prompt_from_ai(text_content, profile=profile, ratio=DEFAULT_IMAGE_RATIO)
        motion = get_runway_ml_video_prompt_from_ai(text_content, image_prompt, model=model)[:512]
        avatar = get_active_avatar(user_id) if user_id else None
        has_avatar = bool(avatar and avatar.get("status") == "succeeded" and avatar.get("model_ref"))
        if is_premium(model):
            if has_avatar:
                image_path = generate_post_image(image_prompt, user_id, ratio="9:16")
                src = create_runway_video(image_path, motion, model=model, ratio="9:16", audio=audio,
                                          user_id=user_id, post_id=post_id)
            else:
                combined = (image_prompt[:700] + " Motion: " + motion)[:980]
                src = create_runway_video(None, combined, model=model, ratio="9:16", audio=audio,
                                          user_id=user_id, post_id=post_id)
        else:
            # Standard tier still uses the account avatar for the source frame when the user has one
            # (avatar likeness regardless of tier; premium only adds the higher-quality Veo motion +
            # credit spend). generate_post_image falls back to base Flux.1 when there is no avatar.
            if has_avatar:
                image_path = generate_post_image(image_prompt, user_id, ratio=DEFAULT_IMAGE_RATIO)
            else:
                image_path = generate_flux1_image_from_prompt(image_prompt, ratio=DEFAULT_IMAGE_RATIO)
            src = create_runway_video(image_path, motion, model=model, ratio=DEFAULT_VIDEO_RATIO,
                                      user_id=user_id, post_id=post_id)
        if not src:
            raise RuntimeError("no video output")
        myprint(f"_generate_video_src: model={model} audio={audio} -> {str(src)[:60]}")
        return src
    except Exception as e:
        myprint(f"_generate_video_src failed ({type(e).__name__}: {e}) — refunding any credits, trying Pexels")
        if deducted and user_id:
            refund_video_credits(user_id, deducted, post_id)
        try:
            from cqc_lem.utilities.pexels_helper import download_pexels_video
            videos_dir = os.path.join(assets_dir, 'videos', 'pexels')
            create_folder_if_not_exists(videos_dir)
            return download_pexels_video(text_content[:50], videos_dir)
        except Exception as pe:
            myprint(f"_generate_video_src: Pexels fallback failed ({type(pe).__name__}: {pe})")
            return None


@attribute_llm_cost(FEATURE_CONTENT)
def create_video_content(user_id: int, stage: str, post_id: int = None) -> tuple[str, str | None]:
    # Get Text Content
    text_content = create_text_post(user_id, stage, post_id=post_id)
    # Load profile once so the image prompt is brand/role-aligned
    user_profile = load_profile_for_user(user_id)
    video_url = _generate_video_src(user_id, text_content, user_profile, post_id)
    return text_content, video_url


def regenerate_video_for_post(post_id: int) -> Optional[str]:
    """Regenerate ONLY the video asset for an existing post, keeping its content.

    Uses the post's existing text content to drive the image->video pipeline
    (RunwayML, with Pexels fallback), saves the result into the shared assets
    volume, updates posts.video_url, and returns the new public asset URL
    (or None if generation failed).
    """
    text_content = get_post_content(post_id)
    if not text_content:
        myprint(f"regenerate_video_for_post: no content for post_id={post_id}")
        return None

    user_id = None
    user_profile = None
    try:
        from cqc_lem.utilities.db import get_post_user_id
        user_id = get_post_user_id(post_id)
        user_profile = load_profile_for_user(user_id)
    except Exception as e:
        myprint(f"regenerate_video_for_post: profile load skipped: {e}")

    # Honors the post's video_quality tier + premium video credits (deduct/refund).
    video_src_url = _generate_video_src(user_id, text_content, user_profile, post_id)
    if not video_src_url:
        return None

    videos_dir = os.path.join(assets_dir, 'videos', 'runwayml')
    create_folder_if_not_exists(videos_dir)
    video_file_path = save_video_url_to_dir(video_src_url, videos_dir)
    # Only AI (Runway, http) output gets C2PA AI credentials — not Pexels stock.
    if str(video_src_url).startswith("http"):
        try:
            from cqc_lem.utilities.c2pa_helper import add_ai_content_credentials
            add_ai_content_credentials(video_file_path)
        except Exception as e:
            myprint(f"regenerate_video_for_post: C2PA signing skipped: {e}")
    video_file_name = os.path.basename(video_file_path)
    api_video_url = f"{API_URL_FINAL}/api/assets?file_name=videos/runwayml/{video_file_name}"
    update_db_post_video_url(post_id, api_video_url)
    # Symmetric with regenerate_post_carousel_task: a real video now exists, so heal a non-terminal
    # post (e.g. one left in 'planning' by asset backfill) to 'approved' so it becomes visible in the
    # Review UI and can post, instead of being stranded despite a correctly-produced video.
    from cqc_lem.utilities.db import get_post_status
    if api_video_url and get_post_status(post_id) != PostStatus.POSTED.value:
        update_db_post_status(post_id, PostStatus.APPROVED)
        myprint(f"regenerate_video_for_post: post {post_id} healed → approved")
    myprint(f"regenerate_video_for_post: post_id={post_id} -> {api_video_url}")
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
        get_post_user_id, get_post_buyer_stage, update_db_post_content,
        get_post_carousel_slides, update_db_post_status, get_post_status,
    )
    user_id = get_post_user_id(post_id)
    if not user_id:
        myprint(f"regenerate_post_carousel_task: no user for post_id={post_id}")
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
            myprint(f"regenerate_post_carousel_task: post {post_id} healed → approved")
    return content


def regenerate_post(post_id: int, guidance: str = None) -> Optional[str]:
    """Regenerate ONE text post in place through the normal generation pipeline — so it honors the
    user's saved engagement settings (voice/tone, focus/goals, emoji/hashtag prefs, anti-self-promo)
    — then folds in optional free-text `guidance`. Mirrors regenerate_newsletter_edition. Resets the
    post to PENDING so the user re-reviews. Text posts only: carousels/videos keep their dedicated
    media healers (regenerate_post_carousel_task / regenerate_post_video_task)."""
    from cqc_lem.utilities.db import get_post_user_id, get_post_buyer_stage, get_post_type

    user_id = get_post_user_id(post_id)
    if not user_id:
        myprint(f"regenerate_post: no user for post_id={post_id}")
        return None
    post_type = get_post_type(post_id)
    pt_value = post_type.value if isinstance(post_type, PostType) else post_type
    # Unknown/None type counts as non-text: regenerating blind could clobber a carousel/video post.
    if pt_value != PostType.TEXT.value:
        myprint(f"regenerate_post: post {post_id} is '{pt_value}', not text — skipping text regenerate")
        return None

    stage = get_post_buyer_stage(post_id) or "awareness"
    # Cached/DB-backed profile (no Selenium) — same source the newsletter regenerate uses.
    user_profile = load_profile_for_user(user_id)
    content = create_text_post(user_id, stage, user_profile=user_profile, post_id=post_id)
    if not content:
        myprint(f"regenerate_post: generation produced nothing for post {post_id}")
        return None

    guidance = (guidance or "").strip()
    if guidance:
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
            myprint(f"regenerate_post: guidance pass failed for post {post_id} ({e}); keeping base regen")

    # Re-score dwell for the regenerated body so the stored score never describes the old draft.
    _score_and_persist_dwell(user_id, post_id, content)
    update_db_post_content(post_id, content)
    update_db_post_status(post_id, PostStatus.PENDING)
    myprint(f"regenerate_post: post {post_id} regenerated → pending")
    return content


@shared_task.task
def regenerate_post_task(post_id: int, guidance: str = None):
    """Celery wrapper so post regeneration (slow lem-complex call) runs async off the API request."""
    return regenerate_post(post_id, guidance)


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
    pipeline."""
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
    signal is never lost."""
    return str(os.getenv("POST_PROOF_REGEN_ENABLED", "on")).strip().lower() in (
        "1", "true", "yes", "on")


def _score_and_persist_authenticity(user_id: int, post_id: int, content: str,
                                    user_profile: LinkedInProfile, profile_synthesis: str = None,
                                    prefs: dict = None) -> None:
    """Run the authenticity gate's LLM judge on a finished draft and persist the 0-100 score (issue
    #382). This only SCORES + records; the demotion to PENDING happens in the content-plan
    status-setter, which reads the score back. No-op when the gate is disabled. Best-effort — a scorer
    or DB hiccup never blocks generation (score_authenticity itself fails open)."""
    if not authenticity_gate_enabled():
        return
    try:
        result = score_authenticity(content, profile=user_profile,
                                    profile_synthesis=profile_synthesis, prefs=prefs)
        update_db_post_authenticity_score(post_id, result.get("score"))
        if result.get("flagged"):
            log_warning(
                f"Post flagged as low-authenticity (score {result.get('score')} < "
                f"{authenticity_score_min()}) — will hold for review: "
                f"{'; '.join(result.get('reasons') or []) or 'no reasons given'}",
                user_id=user_id, post_id=post_id, task_name="create_text_post")
        else:
            log_info(f"Post authenticity score {result.get('score')}",
                     user_id=user_id, post_id=post_id, task_name="create_text_post")
    except Exception as e:
        myprint(f"Authenticity scoring skipped for post {post_id}: {e}")


def _score_and_persist_dwell(user_id: int, post_id: int, content: str) -> Optional[int]:
    """Compute the deterministic dwell-proxy score for a finished post and persist it next to the
    authenticity score (issue #391 — C2). No LLM, no gate: dwell is the dominant 2026 ranking signal
    but our score is a heuristic proxy, so a weak one only logs the specific structural reasons for
    review. Best-effort — a DB hiccup never blocks the pipeline."""
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
        myprint(f"Dwell scoring skipped for post {post_id}: {e}")
        return None


def _review_generated_post(user_id: int, stage: str, post_type: str, user_profile: LinkedInProfile,
                           blueprint: dict, post_id: int, lead_magnet_cta: str, content: str,
                           recent_texts: list, prefs: dict = None,
                           profile_synthesis: Optional[str] = None) -> str:
    """The post-generation REVIEW GATE (the newsletter's dedup maturity applied to posts): compare
    the finished post against the user's recent posts with the deterministic token-set overlap in
    content_framework, AND check the A2 personal-proof slot (a concrete first-person lived detail).
    Too similar (> POST_SIMILARITY_MAX) or missing proof → regenerate ONCE with an explicit
    avoid/proof directive; still failing → log a structured warning and keep the second attempt
    (never loop, never hard-block). Also runs the cheap focus-alignment check on whatever content
    ships."""
    threshold = post_similarity_max()
    score, match = find_most_similar(content, recent_texts)
    too_similar = score > threshold
    missing_proof = not has_first_person_proof(content)
    proof_regen = missing_proof and _proof_regen_enabled()

    if not too_similar and not proof_regen:
        if missing_proof:
            log_warning("Generated post lacks a concrete first-person lived detail (A2 proof slot)",
                        user_id=user_id, post_id=post_id, task_name="create_text_post")
        _check_post_alignment(content, prefs, user_id, post_id, user_profile, profile_synthesis)
        return content

    reasons = []
    if too_similar:
        reasons.append(f"too similar to a recent post (score {score:.2f} > max {threshold:.2f})")
    if proof_regen:
        reasons.append("missing a concrete first-person lived detail (A2 proof slot)")
    myprint(f"Post {'; '.join(reasons)} — retrying once with an explicit avoid/proof directive")

    retry_directive = history_avoidance_directive(
        recent_texts, offending_text=match if too_similar else None)
    if proof_regen:
        retry_directive += personal_proof_directive(profile_synthesis)
    try:
        second = create_text_post(user_id, stage, post_type, user_profile, refine_final_post=True,
                                  blueprint=blueprint, post_id=post_id,
                                  lead_magnet_cta=lead_magnet_cta,
                                  history_directive=retry_directive, similarity_check=False)
    except Exception as e:
        log_warning("Review-gate retry generation failed; keeping first draft", exc=e,
                    user_id=user_id, post_id=post_id, task_name="create_text_post")
        second = None
    if not second:
        _check_post_alignment(content, prefs, user_id, post_id, user_profile, profile_synthesis)
        return content

    second_score, _ = find_most_similar(second, recent_texts)
    if second_score > threshold:
        log_warning(f"Post still similar to a recent post after retry "
                    f"(score {second_score:.2f} > max {threshold:.2f}); keeping second attempt",
                    user_id=user_id, post_id=post_id, task_name="create_text_post")
    if not has_first_person_proof(second):
        log_warning("Post still lacks a concrete first-person lived detail after retry "
                    "(A2 proof slot); keeping second attempt",
                    user_id=user_id, post_id=post_id, task_name="create_text_post")
    _check_post_alignment(second, prefs, user_id, post_id, user_profile, profile_synthesis)
    return second


@attribute_llm_cost(FEATURE_CONTENT)
def create_text_post(user_id: int, stage: str, post_type: str = None, user_profile: LinkedInProfile=None,
                     refine_final_post: bool = True, blueprint: dict = None, post_id: int = None,
                     lead_magnet_cta: str = None, history_directive: str = None,
                     similarity_check: bool = True):
    """
    Generate a text post for LinkedIn based on the user's profile, blog, or website content.

    Parameters:
    - user_id: ID of user to grab user profile
    - stage: Buyer journey stage for the post.

    Returns:
    - str: Generated text post content.
    """

    final_content = None

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
            myprint(f"Error getting user profile: {e}")
            # Prefer the user's cached DB profile (no Selenium) — the old hardcoded "John Doe,
            # Software Developer at ABC Inc." dummy leaked a tech persona into generated posts
            # whenever the live profile load hiccupped. Neutral dummy only as a last resort.
            try:
                user_profile = load_profile_for_user(user_id)
            except Exception as e2:
                myprint(f"Cached profile unavailable: {e2}")
                user_profile = None
            if user_profile is None:
                user_profile = LinkedInProfile(full_name="LinkedIn Member", job_title="Professional")
        finally:
            quit_gracefully(driver)

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
            myprint(f"Could not load recent post history (skipping dedup steering): {e}")
    if history_directive is None:
        history_directive = history_avoidance_directive(recent_texts)

    # ONE assigned SHAPE per post from the SHARED framework core (content_framework): rotate away
    # from this user's recently used post archetypes/hook styles (V51 history) exactly the way
    # newsletter editions rotate — chosen in code, no extra LLM call.
    if blueprint is None:
        try:
            shape_history = get_recent_post_shape_history(user_id)
        except Exception as e:
            myprint(f"Could not load post shape history (rotating without it): {e}")
            shape_history = []
        # Close the feedback loop (issue #389 / B4): bias shape selection away from the user's
        # historically under-performing archetypes/hooks while keeping rotation + exploration.
        try:
            performance = get_shape_performance(user_id)
        except Exception as e:
            myprint(f"Could not load shape performance (selecting without it): {e}")
            performance = None
        blueprint = select_blueprint(
            "post",
            recent_formats=[h.get("archetype") for h in shape_history if h.get("archetype")],
            recent_hook_styles=[h.get("hook_style") for h in shape_history if h.get("hook_style")],
            performance=performance)
    myprint(f"Post blueprint: format={blueprint.get('format')} hook={blueprint.get('hook_style')} "
            f"cta={blueprint.get('cta_style')}")

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
                myprint(f"Lead-magnet CTA included on post_id={post_id} "
                        f"(keyword '{lead_magnet.get('keyword')}')")
        except Exception as e:
            myprint(f"Lead-magnet CTA skipped (settings unavailable): {e}")
            lead_magnet, include_cta, lead_magnet_cta = None, False, ""

    # Generate the post based on the selected type
    myprint(f"Creating text post of type: {post_type} for stage: {stage}")
    if post_type == "thought_leadership":
        final_content = get_thought_leadership_post_from_ai(user_profile, stage, prefs=prefs,
                                                            profile_synthesis=profile_synthesis,
                                                            blueprint=blueprint,
                                                            lead_magnet_cta=lead_magnet_cta,
                                                            post_id=post_id,
                                                            history_directive=history_directive)
    elif post_type == "blog_summary":
        # Get the users blog url
        user_main_blog_url = get_user_blog_url(user_id)
        blog_post_url, blog_post_content = get_main_blog_url_content(user_main_blog_url)
        if blog_post_url and blog_post_content:
            process_selected_post(blog_post_url, blog_post_content)
            final_content = get_blog_summary_post_from_ai(blog_post_url, blog_post_content, user_profile, stage,
                                                          prefs=prefs, profile_synthesis=profile_synthesis,
                                                          blueprint=blueprint, lead_magnet_cta=lead_magnet_cta,
                                                          history_directive=history_directive)
        else:
            myprint("No blog post found for this user. Generating another post type")
            # Chose another random post type that is not "blog_summary"
            post_types.remove("blog_summary")
            post_type = random.choice(post_types)
            final_content = create_text_post(user_id, stage, post_type, user_profile, refine_final_post=False,
                                             blueprint=blueprint, post_id=post_id, lead_magnet_cta=lead_magnet_cta,
                                             history_directive=history_directive, similarity_check=False)
    elif post_type == "website_content":
        # Get the users sitemap url
        sitemap_url = get_user_sitemap_url(user_id)
        if sitemap_url:
            content = generate_website_content_post(sitemap_url, user_profile, stage, prefs=prefs,
                                                    profile_synthesis=profile_synthesis,
                                                    blueprint=blueprint, lead_magnet_cta=lead_magnet_cta,
                                                    history_directive=history_directive)
            if content:
                final_content = content
            else:
                myprint("No relevant content found in the sitemap. Generating another post type")
                # Chose another random post type that is not "website_content"
                post_types.remove("website_content")
                post_type = random.choice(post_types)
                final_content = create_text_post(user_id, stage, post_type, user_profile, refine_final_post=False,
                                             blueprint=blueprint, post_id=post_id, lead_magnet_cta=lead_magnet_cta,
                                             history_directive=history_directive, similarity_check=False)
        else:
            myprint("No sitemap found for this user. Generating another post type")
            # Chose another random post type that is not "website_content"
            post_types.remove("website_content")
            post_type = random.choice(post_types)
            final_content = create_text_post(user_id, stage, post_type, user_profile, refine_final_post=False,
                                             blueprint=blueprint, post_id=post_id, lead_magnet_cta=lead_magnet_cta,
                                             history_directive=history_directive, similarity_check=False)
    elif post_type == "industry_news":
        final_content = get_industry_news_post_from_ai(user_profile, stage, prefs=prefs,
                                                       profile_synthesis=profile_synthesis,
                                                       blueprint=blueprint, lead_magnet_cta=lead_magnet_cta,
                                                       post_id=post_id,
                                                       history_directive=history_directive)
    elif post_type == "personal_story":
        final_content = get_personal_story_post_from_ai(user_profile, stage, prefs=prefs,
                                                        profile_synthesis=profile_synthesis,
                                                        blueprint=blueprint, lead_magnet_cta=lead_magnet_cta,
                                                        post_id=post_id,
                                                        history_directive=history_directive)
    else:
        final_content = generate_engagement_prompt_post(user_profile, stage, prefs=prefs,
                                                        profile_synthesis=profile_synthesis,
                                                        blueprint=blueprint, lead_magnet_cta=lead_magnet_cta,
                                                        post_id=post_id,
                                                        history_directive=history_directive)

    if refine_final_post:
        # Both refinement passes get the user's prefs so the LLM rewrites can't re-introduce
        # emojis/hashtags the user turned off (or flatten a configured tone). CTA-selected posts
        # also pass the keyword so the passes' own anti-bait rules don't paraphrase the sanctioned
        # 'comment KEYWORD' ask into 'reach out for...'.
        _cta_kw = (str(lead_magnet.get("keyword")).strip()
                   if include_cta and lead_magnet else None)
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

    # Humanization pass (issue #416 — A5): the final anti-AI-tell rewrite, BEFORE the authenticity gate
    # so A1 scores the humanized text (generate -> A2 proof -> humanize -> A1 -> review). READER mode
    # only, never fabricates, fails open. Outermost call only (mirrors the A1/review gates). The
    # lead-magnet CTA mechanic is re-verified/repaired by ensure_lead_magnet_cta below, so a rewrite
    # that reworded it is recovered deterministically.
    if final_content and refine_final_post and similarity_check:
        final_content = humanize_text(final_content, content_type="post",
                                      profile_synthesis=profile_synthesis, prefs=prefs)

    # Authenticity gate (issue #382 — 360Brew defense): score the finished draft for generic-AI risk
    # + profile-topic consistency and persist the score BEFORE the similarity gate. The gate itself is
    # a status demotion (APPROVED -> PENDING) applied by the content-plan status-setter, which reads
    # the persisted score back — mirroring the deterministic similarity gate's demote-not-block posture.
    if final_content and refine_final_post and similarity_check and post_id is not None:
        _score_and_persist_authenticity(user_id, post_id, final_content, user_profile,
                                        profile_synthesis, prefs)

    # Review gate (runs once, in the outermost call): deterministic near-duplicate check against the
    # user's recent posts with ONE avoid-directive retry, plus a cheap focus-alignment check. Never
    # a hard block — worst case it logs a warning and keeps the best attempt.
    if final_content and refine_final_post and similarity_check:
        final_content = _review_generated_post(user_id, stage, post_type, user_profile, blueprint,
                                               post_id, lead_magnet_cta, final_content,
                                               recent_texts, prefs, profile_synthesis)

        # Dwell shaping (issue #391 — C2): deterministic, no-LLM repair of the structural dwell
        # killer every LLM rewrite above can re-introduce — wall-of-text paragraphs. Runs on
        # whichever draft the review gate kept, reflows ONLY over-long paragraphs, and never
        # truncates, so the CTA/proof/hashtag lines survive. The hook-before-fold and read-time
        # targets are steered in the prompt (content_framework.dwell_directive) and measured into
        # the persisted dwell-proxy score when the post is saved.
        final_content = shape_for_dwell(final_content)

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

    # Persist the assigned shape so FUTURE posts rotate away from it (the newsletter's V50 shape
    # history, applied to posts via V51). Only the outermost call (which knows the post row) writes.
    if post_id is not None and final_content and blueprint:
        try:
            update_db_post_shape(post_id, blueprint.get("format"), blueprint.get("hook_style"),
                                 topic=blueprint.get("subject"))
        except Exception as e:
            myprint(f"Could not persist post shape for post {post_id}: {e}")

    return final_content


def get_main_blog_url_content(blog_url):
    """
    Retrieve recent posts from a blog, randomly select one, and send the post content to another function.

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
            myprint("WordPress blog detected. Fetching posts via API...")
        else:
            # If the API is not available, fall back to web scraping
            myprint("Non-WordPress blog detected or API unavailable. Fetching posts via web scraping...")
            recent_posts = scrape_recent_posts(blog_url)

    except requests.exceptions.HTTPError as http_err:
        myprint(f"HTTP error occurred: {http_err}")
        myprint("Falling back to web scraping...")
        # In case of any request failure, fall back to web scraping
        recent_posts = scrape_recent_posts(blog_url)
    except requests.exceptions.ConnectionError as conn_err:
        myprint(f"Connection error occurred: {conn_err}")
    except requests.exceptions.Timeout as timeout_err:
        myprint(f"Timeout error occurred: {timeout_err}")
    except requests.exceptions.RequestException as req_err:
        myprint(f"Some other request error occurred: {req_err}")
        myprint("Falling back to web scraping...")
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
            myprint(f"Error parsing post content: {post_content}")

        # myprint(f"Randomly selected post content: {post_content}")
        post_url = selected_post.get("link", blog_url)

        return post_url, post_content
    else:
        myprint("No recent posts found or unable to fetch posts.")
        return None, None


def scrape_recent_posts(blog_url):
    """
    Fallback method to scrape recent posts from a non-WordPress blog.

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
        myprint(f"Error fetching or parsing blog page: {e}")

    if len(recent_posts) == 0:
        myprint(f"No recent posts found on the blog page: {blog_url}")
    else:
        myprint(f"Found {len(recent_posts)} post(s) from: {blog_url}")

    return recent_posts


def process_selected_post(url, content):
    """
    Process the selected post's content and URL.

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
    myprint(f"Selected Post URL: {url}")
    if content:
        myprint(f"Selected Post Content: {content[:200]}...")  # Print the first 200 characters for preview
    else:
        myprint("Selected Post Content: None")


def generate_website_content_post(sitemap_url, linked_user_profile, stage: str, prefs: dict = None,
                                  profile_synthesis: Optional[str] = None, blueprint: dict = None,
                                  lead_magnet_cta: str = None, history_directive: str = None):
    """
    Generate a post based on content found on the user's website using their sitemap url catered to readers in the desired buyers journey stage.
    Scrapes or retrieves key points from the website's sitemap.
    """

    # 1. Fetch and parse the sitemap XML
    page_urls = fetch_sitemap_urls(sitemap_url)
    myprint(f"Founds {len(page_urls)} URLs from sitemap {sitemap_url}")

    # 2. Filter URLs that are likely to contain shareable content
    relevant_urls = filter_relevant_urls(page_urls)
    myprint(f"Found {len(relevant_urls)} relevant URLs in the sitemap.")

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
                                                                 history_directive=history_directive)
            return social_media_post
        else:
            myprint("No content extracted from the selected URL.")
            return None
    else:
        myprint("No relevant URLs found in the sitemap.")
        return None


def get_session_for_response() -> Tuple[requests.Session, dict]:
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
    session, headers = get_session_for_response()

    content = None

    try:
        # Make the request
        response = session.get(url, headers=headers, timeout=10)

        # Raise an exception for HTTP errors
        response.raise_for_status()

        content = response.content

    except requests.exceptions.HTTPError as http_err:
        myprint(f"HTTP error occurred: {http_err}")
    except requests.exceptions.ConnectionError as conn_err:
        myprint(f"Connection error occurred: {conn_err}")
    except requests.exceptions.Timeout as timeout_err:
        myprint(f"Timeout error occurred: {timeout_err}")
    except requests.exceptions.RequestException as req_err:
        myprint(f"Some other request error occurred: {req_err}")

    # Return the content
    return content


def fetch_sitemap_urls(sitemap_url):
    """
    Fetch and parse the sitemap XML to get a list of URLs, including any sub-sitemaps.

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
                    myprint(f"Skipping sub-sitemap: {sub_sitemap_url}")
                    continue
                myprint(f"Fetching sub-sitemap: {sub_sitemap_url}")
                urls.extend(fetch_sitemap_urls(sub_sitemap_url))  # Recursive call

    except requests.RequestException as e:
        print(f"Error fetching sitemap: {e}")
    except ElementTree.ParseError as e:
        print(f"Error parsing sitemap XML: {e}")

    return urls


def filter_relevant_urls(urls: list[str], by_blog_post_check=True, max_list_size=10):
    """
    Filter the URLs to keep only those that are likely to be a blog post or contain shareable content.
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
    """
    Extract key content from the page, such as the title, main points, or summary.
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
        myprint(f"Error fetching page content: {e}")
        return None, None


def save_content_plan(user_id: int, daily_plan: list[dict]):
    """Save the planned content schedule to the database."""
    # Insert the 'daily_plan' into a database table for future reference and scheduling
    for plan in daily_plan:
        # Convert plan['post_type'] to Post Type object
        post_type = PostType[plan['post_type'].upper()]
        insert_planned_post(user_id, plan['scheduled_datetime'], post_type, plan['stage'])


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
        myprint(f"Creating buffer content for user id: {user_id}")
        user_ids = [user_id]
    else:
        user_ids = get_user_ids_with_planned_posts_within_buffer() or []

    for uid in user_ids:
        prefs = get_user_preferences(uid) or {}
        buffer_days, buffer_max = _content_buffer_settings(prefs)
        already_ready = count_ready_posts_within_buffer(uid, buffer_days)
        if already_ready >= buffer_max:
            myprint(f"user_id {uid}: buffer already full ({already_ready}/{buffer_max} ready "
                    f"within {buffer_days} days). Skipping content creation.")
            continue

        planned_posts = get_planned_posts_within_buffer(uid, buffer_days, buffer_max, already_ready)
        if not planned_posts:
            myprint(f"user_id {uid}: no planned posts within {buffer_days} days. "
                    f"Skipping content creation.")
            continue

        myprint(f"user_id {uid}: generating {len(planned_posts)} post(s) to top buffer up to "
                f"{buffer_max} ready within {buffer_days} days ({already_ready} already ready)")
        for post in planned_posts:
            _create_content_for_planned_post(post, prefs)


def _create_content_for_planned_post(post: dict, prefs: dict) -> None:
    """Generate, store and status one planning post. Never raises — a bad post is skipped."""
    user_id = post['user_id']
    post_id = post['id']
    post_type = post['post_type']
    stage = post['buyer_stage']

    try:
        content, video_url = create_content(user_id, post_type, stage, post_id=post_id)
    except Exception as e:
        myprint(f"Skipping post_id {post_id}: content generation raised {type(e).__name__}: {e}")
        return

    if content is None:
        myprint(f"Skipping post_id {post_id}: content generation returned None")
        return

    # Copy the video from url to our assets/video folder and store it to the database for later retrieval via api call
    ai_video = False
    if video_url:
        # AI (Runway) output is a remote http URL; Pexels fallback is a local path.
        ai_video = str(video_url).startswith("http")
        # Define and create assets_dir / videos
        videos_dir = os.path.join(assets_dir, 'videos', 'runwayml')
        create_folder_if_not_exists(videos_dir)
        video_file_path = save_video_url_to_dir(video_url, videos_dir)
        myprint(f"Video from url: {video_url} | Saved to: {video_file_path}")
        # Attach AI Content Credentials to AI-generated video only (not stock).
        if ai_video:
            try:
                from cqc_lem.utilities.c2pa_helper import add_ai_content_credentials
                add_ai_content_credentials(video_file_path)
            except Exception as e:
                myprint(f"C2PA signing skipped for post_id={post_id}: {e}")
        # Get the file name from the video file path
        video_file_name = os.path.basename(video_file_path)

        # The video url is our api prefix + 'assets?file=videos/runwayml' +  video_file_name
        api_video_url = f"{API_URL_FINAL}/api/assets?file_name=videos/runwayml/{video_file_name}"
        myprint(f"Video URL: {api_video_url}")

        # Update the database with the video url
        update_db_post_video_url(post_id, api_video_url)

    # Disclose AI-generated visuals in the caption (caption-line fallback for C2PA)
    if ai_video:
        content = _apply_ai_disclosure(content)

    # Dwell-proxy score (issue #391 — C2) for EVERY post type: the text post, the carousel
    # caption, and the video caption all live or die on holding the reader past the fold.
    # Deterministic and advisory — recorded next to the authenticity score, never a gate.
    _score_and_persist_dwell(user_id, post_id, content)

    # Update the database with the created content
    myprint(f"Updating content for post_id: {post_id}")
    update_db_post_content(post_id, content)

    # Respect the user's auto_schedule_posts preference (fetched once per user by the caller):
    # True → APPROVED (Celery will pick it up); False → PENDING (manual review required)
    auto_schedule = bool((prefs or {}).get("auto_schedule_posts", True))
    new_status = PostStatus.APPROVED if auto_schedule else PostStatus.PENDING

    # Never auto-approve a video/carousel post whose media failed to generate — hold it
    # PENDING so the backfill task (or manual review) can complete the asset before it
    # can be scheduled/posted. Prevents assetless posts going out.
    if _post_missing_required_asset(post_id, post_type, video_url):
        new_status = PostStatus.PENDING
        myprint(f"post_id {post_id}: required media asset missing — holding PENDING")

    # Authenticity gate (issue #382): demote an auto-approve to PENDING when the persisted
    # LLM-judged authenticity score (set by create_text_post's scoring step) is below threshold,
    # so a generic-AI-flagged draft gets human review before it can post. Only downgrades an
    # APPROVED — never upgrades a PENDING, and never blocks when unscored (score None).
    if new_status == PostStatus.APPROVED and authenticity_gate_enabled():
        score = get_post_authenticity_score(post_id)
        if score is not None and score < authenticity_score_min():
            new_status = PostStatus.PENDING
            log_warning(f"post_id {post_id}: authenticity score {score} < "
                        f"{authenticity_score_min()} — holding PENDING for review",
                        user_id=user_id, post_id=post_id, task_name="create_content")

    myprint(f"Updating post_id: {post_id} Status={new_status}")
    update_db_post_status(post_id, new_status)


def is_blog_post(url):
    parsed = urlparse(url)
    path = parsed.path.strip('/')

    # Check if the path contains typical blog indicators
    if any(segment.isdigit() for segment in path.split('/')) or len(path.split('/')) > 1:
        return True
    return False


def is_blog_post_by_metadata(url):
    try:
        content = fetch_content(url)
        # myprint(f"Fetched URL: {url}: Content: {content}")
        soup = BeautifulSoup(content, 'html.parser')

        # Look for common blog-related tags
        # if soup.find('article') or soup.find('meta', {'name': 'author'}):
        if soup.find('meta', {'name': 'author'}):
            return True
    except Exception as e:
        myprint(f"Error fetching URL: {e}")
    return False


def is_blog_post_combined(url):
    if is_blog_post(url):
        return True
    if is_blog_post_by_metadata(url):
        return True
    return False


if __name__ == '__main__':
    myprint("Generating content plan for 30 days")
    auto_generate_content()
    myprint("Creating weekly content")
    auto_create_weekly_content()
    myprint("Process finished")
