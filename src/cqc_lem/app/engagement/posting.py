"""Posting and the read-only sweeps that follow a post — publish it, then measure what it earned.

Step 4 of the `run_automation.py` split (#1154). Posting and sweeps move as ONE module because they
are one graph: `post_to_linkedin` publishes, and every sweep here reads back what that publish
produced — replies under it, follow-ups to those replies, the comment outcomes at T+24h, and the
post/audience stats. Measured before anything moved: 10 tasks, 72 symbols, and **zero** symbols
shared with the outreach/DM remainder, so nothing had to be left behind.

**Every task here pins `name='cqc_lem.app.run_automation.<fn>'`, and that is load-bearing.** Celery
derives a task's name from `<module>.<function>`, so moving one RENAMES it silently: four of these
ten are named as plain strings in `celeryconfig.task_routes` and would stop matching, messages
already queued under the old name would be rejected `NotRegistered` and dropped, and the `QueueOnce`
lock key embeds the task name, so it would re-key mid-deploy — for `post_to_linkedin`, whose lock is
keyed on `post_id`, that means publishing the same post twice.
`scripts/restructure/celery_inventory.py` diffed across the move is what proves none of that happened.

`automate_reply_commenting` re-queues ITSELF with `globals()[current_function_name].apply_async`.
`current_function_name` is `frame.f_code.co_name`, so the lookup reads THIS module's globals and
stays correct — but only because the task and its module moved together. Never split or wrap it.

The module imports NOTHING from `run_automation` — that is what keeps the dependency one-way, since
`run_automation` imports the ten tasks back for `run_scheduler` and `api/*` to keep reading by name.
The one edge that runs the other way is a WIRE edge: `post_to_linkedin` dispatches the seed comment
and the second wave into `app.engagement.feed` by `.apply_async`, never by calling them.

Posture for every lane below — the golden-hour reply amplifier (#622/#401), the comment-quality
contract on a reply (#617), the owned-asset CTA loop (#624), comment outcomes and the demotion hold
(#628) — is `docs/engagement-automation.md`.
"""

import hashlib
import inspect
import os
import random
import re
import time
from datetime import datetime, timezone
from typing import Optional, Tuple
from urllib.parse import urlparse

from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By

# `post_to_linkedin` dispatches both self-comments into the feed cluster (#1154) by `.apply_async` —
# a name on a message, not a call — and feed imports nothing back, so the edge runs one way.
from cqc_lem.app.engagement.feed import auto_second_wave_comment, auto_seed_comment_on_post
from cqc_lem.app.my_celery import app as shared_task
from cqc_lem.app.queue_once import QueueOnce
from cqc_lem.utilities import golden_hour as _golden
from cqc_lem.utilities.ai.ai_helper import (
    generate_comment_reply_followup,
    generate_thread_reply,
    get_or_create_profile_synthesis,
    synthesize_profile,
)
from cqc_lem.utilities.ai.content_alignment import (
    ARTIFACT_KIND_LEAD_MAGNET,
    resolve_artifact_delivery,
    split_link_for_first_comment,
)
from cqc_lem.utilities.audience_stats import (
    parse_connection_count,
    parse_follower_count,
    parse_profile_views,
    parse_search_appearances,
)
from cqc_lem.utilities.db import (
    SCHEDULED_DM_SOURCE_ARTIFACT,
    SCHEDULED_DM_SOURCE_NURTURE,
    LeadSignalChannel,
    LeadSignalSource,
    LogActionType,
    LogResultType,
    PostStatus,
    PostType,
    ScheduledDmStatus,
    count_followup_replies_today,
    count_scheduled_dms_created_today,
    get_carousel_slides,
    get_comment_followup,
    get_comment_outcome_targets,
    get_engagement_preferences,
    get_lead_magnet_settings,
    get_linkedin_profile_url_by_user_id,
    get_post_content,
    get_post_manual_publish,
    get_post_message_from_log_for_user,
    get_post_status,
    get_post_type,
    get_post_url_from_log_for_user,
    get_post_video_url,
    get_recent_commented_rows_with_text,
    get_recent_navigable_commented_posts,
    get_recent_posted_post_ids,
    get_shipped_variant_keys,
    get_uncaptured_posted_post_ids,
    get_user_blog_url,
    get_user_password_pair_by_id,
    has_open_scheduled_dm,
    has_received_lead_magnet,
    insert_new_log,
    insert_scheduled_dm,
    record_comment_followup,
    record_comment_outcome,
    record_follower_stat,
    record_lead_magnet_sent,
    record_post_stats,
    set_profile_synthesis,
    update_commented_post_key,
    update_db_post_content,
    update_db_post_first_comment_link,
    update_db_post_status,
    upsert_engager,
)
from cqc_lem.utilities.dm_templates import render_dm_placeholders
from cqc_lem.utilities.golden_hour import _record_golden_hour_report, _reply_outcome
from cqc_lem.utilities.human_pacing import (
    ACTION_REPLY,
    record_action,
)
from cqc_lem.utilities.lead_scoring import (
    _flag_lead_signal,
    _href_is_profile,
    profile_slug,
)
from cqc_lem.utilities.linkedin import zero_walk as _zw

# The SDUI mechanics every engagement cluster shares moved down to `utilities/linkedin/*` (#1154).
# They are imported by their ORIGINAL names, underscore and all: the bodies moved verbatim, so one
# spelling still greps to one place, and the test patches that follow them are a pure module-path
# change. Nothing here is re-exported — a symbol this module no longer reads is simply gone, so a
# stale `patch("...run_automation._card_for_textbox")` raises AttributeError instead of binding a
# name nothing reads and passing having tested nothing.
from cqc_lem.utilities.linkedin.cards import (
    _BARE_COUNT_RE,
    _FEED_POST_TEXT_SEL,
    _X_LOWER_ARIA,
    _X_LOWER_TEXT,
    _card_for_textbox,
    _feed_post_urn_from_card,
    _norm_prefix,
    _normalize_post_text,
    _parse_count,
    _post_permalink_from_card,
    _post_social_counts,
)
from cqc_lem.utilities.linkedin.composer import (
    _comment_items,
    _comment_items_from_thread,
    _reply_composer_for_comment,
    _reply_under_comment_inline,
    _type_and_submit_reply,
)
from cqc_lem.utilities.linkedin.helper import (
    clean_person_name,
    connection_degree,
)
from cqc_lem.utilities.linkedin.poster import (
    object_urn_from_post_url,
    share_carousel_on_linkedin,
    share_document_on_linkedin,
    share_on_linkedin,
)
from cqc_lem.utilities.linkedin.rate_limit import (
    LinkedInRateLimited,
    _redis_client,
    acquire_run_lock,
    release_run_lock,
)
from cqc_lem.utilities.linkedin.session import get_current_profile
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning
from cqc_lem.utilities.observability import (
    FEATURE_COMMENT,
    llm_attribution,
    track_audience_snapshot,
    track_comment_outcome,
    track_post_outcome,
)
from cqc_lem.utilities.selenium_util import (
    click_first,
    find_first,
    quit_gracefully,
)

# ── zero-walk tripwire (issues #1013, #1021) ────────────────────────────────────────────────────
# The grading itself lives in utilities/linkedin/zero_walk.py. `run_automation` keeps its own alias
# of the same module for the catch-up walk: aliasing the upstream original in BOTH modules is what
# lets neither import the other (#1154). Kept under the name the moved body already used.
_grade_zero_walk = _zw.grade_zero_walk


# The zero-walk cross-check for a stats read that scored EVERY signal 0 (issue #1021): a post with
# no engagement and a post whose layout the parser no longer matches look identical in the numbers.
# Deliberately a DIFFERENT vocabulary from _STACKED_LABEL_FIRST — a cross-check that only knows the
# labels the parser maps could never see the rename that broke it — and it demands a NON-ZERO count
# beside the label, so a genuinely quiet post ("Impressions / 0") reads `empty`, never drift.
_CROSSCHECK_LABEL_RE = re.compile(
    r"^(?:reaction|like|comment|repost|share|save|impression|view|member[s]?\s+reached)s?$",
    re.IGNORECASE)


def _rendered_count_signals(text: str) -> int:
    """How many engagement counts the PAGE renders as a non-zero number beside its own label.

    Shares the parser's adjacency assumption but not its label map: a layout that moves a value away
    from its label counts 0 here, which grades `empty` — the fail-safe direction for a tripwire.
    """
    lines = [line.strip() for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    found = 0
    for i, line in enumerate(lines):
        if not _CROSSCHECK_LABEL_RE.match(line.rstrip(":")):
            continue
        neighbours = ([lines[i - 1]] if i else []) + ([lines[i + 1]] if i + 1 < len(lines) else [])
        if any(_BARE_COUNT_RE.match(n) and _parse_count(n) > 0 for n in neighbours):
            found += 1
    return found


def _main_text(driver) -> "str | None":
    """The rendered <main> text, or None when it cannot be read at all."""
    try:
        return driver.find_element(By.TAG_NAME, "main").text or ""
    except Exception:
        return None


def _reply_to_comment_inline(driver, wait, comment_el, reply_text: str, user_id: int = None) -> bool:
    """Open a comment's inline reply box, type the reply, and submit (same SDUI pattern as
    post_comment_inline: role=textbox composer + Ctrl+Enter fallback). The composer is resolved
    against THIS comment (`_reply_composer_for_comment`), never page-wide. Returns True if posted.
    """
    try:
        if click_first(driver, wait, [(By.CSS_SELECTOR, "button[aria-label='Reply']")],
                       "Open reply box", parent_element=comment_el, required=False, user_id=user_id) is None:
            return False
        time.sleep(random.uniform(1.5, 3))
        composer = _reply_composer_for_comment(driver, comment_el, user_id=user_id)
        if composer is None:
            return False
        return _type_and_submit_reply(driver, composer, reply_text, user_id=user_id)
    except Exception as e:
        log_warning("Inline reply post failed", exc=e, action_type="reply", user_id=user_id)
        return False


_ANALYTICS_URL = "https://www.linkedin.com/analytics/post-summary/{urn}/"


def _post_analytics_counts(driver, post_url: str) -> dict:
    """Counts from the author's own post-analytics page. Prefers the URN the detail page redirected
    to (the logged permalink holds a share/ugcPost URN; analytics keys off the activity URN LinkedIn
    resolves it to). Best-effort — {} when no URN, no page, or nothing parseable.
    """
    try:
        current = getattr(driver, "current_url", None)
        urn = (object_urn_from_post_url(current if isinstance(current, str) else "")
               or object_urn_from_post_url(post_url or ""))
        if not urn:
            return {}
        driver.get(_ANALYTICS_URL.format(urn=urn))
        time.sleep(random.uniform(4, 6))
        container = driver.find_element(By.TAG_NAME, "main")
        return {k: v for k, v in _post_social_counts(container).items() if v}
    except Exception as e:
        log_warning("Post analytics page unavailable", exc=e, task_name="auto_scrape_post_stats")
        return {}


def _post_stats_backfill_bounds() -> Tuple[int, int]:
    """(window days, per-run cap) for the never-captured backfill (issue #809). A typo'd env value
    falls back to the defaults rather than taking the whole sweep down.
    """
    def _read(name: str, default: int) -> int:
        try:
            return max(0, int((os.environ.get(name) or "").strip() or default))
        except ValueError:
            return default
    return _read("POST_STATS_BACKFILL_DAYS", 90), _read("POST_STATS_BACKFILL_MAX", 5)


@shared_task.task(name='cqc_lem.app.run_automation.auto_scrape_post_stats',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_content')
def auto_scrape_post_stats(self, user_id: int):
    """Capture reactions/comments/reposts/impressions/saves for each of the user's recent posts
    (feeds personalized post-time recommendations + the content feedback loop). Reuses the
    social-count extraction on each post's detail page, then on its analytics page for the
    signals the detail page never renders (saves, impressions).
    """
    post_ids = get_recent_posted_post_ids(user_id)
    # The analytics dashboard reads a 90-day window while this sweep only walks the last few weeks,
    # so a post that missed its capture while it was fresh stayed unmeasurable forever (issue #809).
    # Top the sweep up with a bounded number of never-captured posts from the wider window.
    backfill_days, backfill_max = _post_stats_backfill_bounds()
    if backfill_max:
        already = set(post_ids)
        post_ids = post_ids + [pid for pid in get_uncaptured_posted_post_ids(
            user_id, days=backfill_days, limit=backfill_max) if pid not in already]
    if not post_ids:
        return "No recent posts to scrape"
    # One query for the whole sweep, so every outcome event can name the A/B variant its post shipped
    # (issues #396/#652) without a lookup inside the Selenium loop.
    shipped_variants = get_shipped_variant_keys(user_id)
    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Post Stats",
                                                                   measurement_only=True)
    except Exception as e:
        log_error("Error getting profile for post stats", exc=e, user_id=user_id, task_name="auto_scrape_post_stats")
        return f"Failed: {e}"
    scraped = 0
    try:
        for pid in post_ids:
            url = get_post_url_from_log_for_user(user_id, pid)
            if not url:
                continue
            driver.get(url)
            time.sleep(random.uniform(4, 6))
            try:
                container = driver.find_element(By.TAG_NAME, "main")
            except Exception:
                container = None
            detail_text = _main_text(driver) if container is not None else None
            counts = _post_social_counts(container) if container is not None else {}
            # The detail page's social bar carries reactions/comments/reposts; saves and a reliable
            # impression count exist ONLY on the author's analytics page — merge by max so a signal
            # the analytics view doesn't render can't zero out one the detail page did.
            for key, val in _post_analytics_counts(driver, url).items():
                counts[key] = max(counts.get(key) or 0, val)
            # NOTHING parsed (a readable page always yields the full zero-filled dict) means the page
            # never rendered — auth wall, 429, dead permalink — not that the post earned nothing.
            # Recording those zeros publishes a fabricated row to the analytics panel, and for a
            # backfilled post it is permanent: one bad read retires it from the never-captured queue
            # and the dashboard measures a lie instead of a gap (#809).
            if not counts:
                log_debug("Post page unreadable — leaving it uncaptured", user_id=user_id,
                          post_id=pid, task_name="auto_scrape_post_stats")
                continue
            # An all-zero read is the OTHER fabricated row (#1021): a quiet post and a rotated
            # layout score identically. Ask the page — the analytics view is the one on screen now,
            # so both texts are cross-checked — and leave a drifted read uncaptured for the same
            # reason an unreadable one is left: a written zero is permanent for a backfilled post.
            if not any(counts.values()):
                texts = [text for text in (detail_text, _main_text(driver)) if text is not None]
                native = sum(_rendered_count_signals(text) for text in texts) if texts else None
                if _grade_zero_walk(native, "Post social-count parse", user_id=user_id,
                                    post_id=pid, task_name="auto_scrape_post_stats") == "drift":
                    continue
            record_post_stats(user_id, pid, counts.get("reactions", 0), counts.get("comments", 0),
                              reposts=counts.get("reposts") or 0,
                              impressions=counts.get("impressions") or None,
                              saves=counts.get("saves") or 0)
            track_post_outcome(post_id=pid, reactions=counts.get("reactions", 0),
                               comments=counts.get("comments", 0), reposts=counts.get("reposts") or 0,
                               impressions=counts.get("impressions") or None,
                               saves=counts.get("saves") or 0, user_id=user_id,
                               variant_key=shipped_variants.get(pid))
            scraped += 1
        return f"Scraped stats for {scraped} post(s)"
    finally:
        quit_gracefully(driver)


# The author's OWN analytics surface. Profile views and search appearances are rendered as summary
# cards on the profile-views page, so that one load usually yields both; the search-appearances page
# is only opened when it didn't.
_PROFILE_VIEWS_URL = "https://www.linkedin.com/analytics/profile-views/"
_SEARCH_APPEARANCES_URL = "https://www.linkedin.com/analytics/search-appearances/"


def _read_page_text(driver, url: str) -> str:
    """Navigate and return the page's visible text (prefers <main>, falls back to <body>). Empty
    string when the page can't be read — never raises.
    """
    try:
        driver.get(url)
    except Exception as e:
        log_warning("Could not open page for audience capture", exc=e, task_name="capture_follower_stats")
        return ""
    time.sleep(random.uniform(4, 6))
    for tag in ("main", "body"):
        try:
            text = driver.find_element(By.TAG_NAME, tag).text or ""
        except Exception:
            continue
        # An EMPTY <main> is as unread as a missing one (LinkedIn renders parts of the analytics
        # surface outside it, and a half-hydrated shell reads blank), so keep falling back.
        if text.strip():
            return text
    return ""


def capture_audience_snapshot(driver, profile_url: "str | None") -> dict:
    """Read the user's follower/connection counts off their own profile and their profile-view /
    search-appearance counts off their own analytics surface (issue #627).

    Best-effort PER SIGNAL and fail-closed: a missing anchor leaves that key None (recorded as SQL
    NULL = "not measured"), never 0, and never raises — a LinkedIn DOM change must not take the
    daily capture, or anything downstream of it, down with it.
    """
    counts = {"follower_count": None, "connection_count": None,
              "profile_views": None, "search_appearances": None}
    if profile_url:
        text = _read_page_text(driver, profile_url)
        counts["follower_count"] = parse_follower_count(text)
        counts["connection_count"] = parse_connection_count(text)
    else:
        log_warning("No LinkedIn profile URL — skipping follower capture",
                    task_name="capture_follower_stats")
    analytics_text = _read_page_text(driver, _PROFILE_VIEWS_URL)
    counts["profile_views"] = parse_profile_views(analytics_text)
    counts["search_appearances"] = parse_search_appearances(analytics_text)
    if counts["search_appearances"] is None:
        counts["search_appearances"] = parse_search_appearances(
            _read_page_text(driver, _SEARCH_APPEARANCES_URL))
    return counts


@shared_task.task(name='cqc_lem.app.run_automation.capture_follower_stats',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_content')
def capture_follower_stats(self, user_id: int):
    """Daily audience telemetry: snapshot the user's follower/connection counts and (when the
    surface is reachable) their profile views + search appearances (issue #627). Audience growth is
    the outcome the whole system exists to produce and was previously untracked. One row per run
    feeds the growth panel's 7/30-day deltas; unreadable signals are stored as NULL.
    """
    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id,
                                                                  session_name="Audience Stats",
                                                                  measurement_only=True)
    except Exception as e:
        log_error("Error getting profile for audience capture", exc=e, user_id=user_id,
                  task_name="capture_follower_stats")
        return f"Failed: {e}"
    try:
        profile_url = get_linkedin_profile_url_by_user_id(user_id)
        if not profile_url:
            candidate = getattr(my_profile, "profile_url", None)
            profile_url = str(candidate) if candidate else None
        counts = capture_audience_snapshot(driver, profile_url)
        if not record_follower_stat(user_id, **counts):
            log_warning("No audience signal readable — snapshot skipped", user_id=user_id,
                        task_name="capture_follower_stats")
            return "No audience signals readable"
        track_audience_snapshot(user_id=user_id, **counts)
        log_info("Audience snapshot captured", user_id=user_id, task_name="capture_follower_stats")
        return (f"Followers: {counts['follower_count'] if counts['follower_count'] is not None else 'unknown'}; "
                f"profile views: {counts['profile_views'] if counts['profile_views'] is not None else 'unknown'}")
    except Exception as e:
        log_error("Audience capture error", exc=e, user_id=user_id, task_name="capture_follower_stats")
        return f"Audience capture error: {e}"
    finally:
        quit_gracefully(driver)


# Max replies posted per post in a single sweep — a volume backstop so a huge comment thread (or an
# unexpected re-trigger) can never fire an unbounded burst. Already-replied comments are skipped
# regardless, so this only ever caps NEW replies.
_MAX_REPLIES_PER_SWEEP = 15

# Golden-hour reply amplifier (#401): the first ~hour after publishing is the top 2026 reach window,
# so on event mode we sweep own-post comments repeatedly across it instead of once — every comment
# left while the post is still being distributed gets a timely, substantive reply. Sweep count is
# env-tunable (GOLDEN_HOUR_REPLY_SWEEPS); each sweep is QueueOnce + 429-safe, so an extra/overlapping
# run is harmless and a rate-limited session skips cleanly.
# The timing decisions themselves live in utilities/golden_hour.py (issue #622) so they can be tested
# without importing the task module; these names stay as the in-module vocabulary.
_GOLDEN_HOUR_MINUTES = _golden.GOLDEN_HOUR_MINUTES  # lgtm[py/unused-global-variable]
_GOLDEN_HOUR_REPLY_SWEEPS = _golden.GOLDEN_HOUR_REPLY_SWEEPS  # lgtm[py/unused-global-variable]
_GOLDEN_HOUR_MAX_SWEEPS = _golden.GOLDEN_HOUR_MAX_SWEEPS  # lgtm[py/unused-global-variable]
_golden_hour_sweep_countdowns = _golden.sweep_countdowns


# --- inbound hot-lead detection (issue #483) ---------------------------------------------------
# Every read path below already HAS the text — someone asking "how much?" or "can you help with X?"
# is the warmest lead we get, and we were dropping it. Detection rides those existing reads: no new
# scraping, no extra navigation. A hit records a lead_signals row with an APPROVAL-GATED draft.
_MAX_LEAD_FLAGS_PER_SWEEP = 10  # volume backstop: a draft costs an LLM call, so bound them per run


def _reply_target_key(user_id: int, post_id: int, commenter_slug: str, comment_text: str) -> str:
    """Stable Redis key for a specific comment on a specific post so the reply sweep never replies
    to the SAME comment twice across golden-hour sweeps. Keyed on identity (commenter slug) + a
    normalized prefix of the comment text — the same inputs the follow-up path uses for its dedup.
    If we cannot resolve the commenter we fall back to a text-only hash so the dedup still has a
    chance to prevent duplicates.
    """
    text_norm = _normalize_post_text(comment_text)[:200]
    who = (commenter_slug or "").strip().lower()
    if not who:
        who = hashlib.sha1(text_norm.encode("utf-8", "ignore")).hexdigest()[:16]
    digest = hashlib.sha1(f"{who}|{text_norm}".encode("utf-8", "ignore")).hexdigest()[:20]
    return f"linkedin:replied_to_own_comment:{user_id}:{post_id}:{digest}"


def _has_replied_to_comment(user_id: int, post_id: int, commenter_slug: str, comment_text: str) -> bool:
    """True if we have already recorded a reply to this comment (best-effort Redis check; fails
    open when Redis is unavailable).
    """
    client = _redis_client()
    if client is None:
        return False
    try:
        return bool(client.get(_reply_target_key(user_id, post_id, commenter_slug, comment_text)))
    except Exception:
        return False


def _record_replied_to_comment(user_id: int, post_id: int, commenter_slug: str, comment_text: str,
                               ttl_days: int = 3) -> None:
    """Mark a comment as replied-to in Redis so later sweeps skip it. TTL covers the reply look-back
    window plus a small buffer; clamped so a misconfigured value can't pin keys forever.
    """
    client = _redis_client()
    if client is None:
        return
    ttl_seconds = max(1, min(15 * 24 * 60 * 60, int(ttl_days) * 24 * 60 * 60))
    try:
        client.set(_reply_target_key(user_id, post_id, commenter_slug, comment_text), "1", ex=ttl_seconds)
    except Exception as e:
        log_warning("Could not record replied-to-comment marker", exc=e, user_id=user_id,
                    post_id=post_id, action_type="reply")


def _queue_artifact_delivery(user_id: int, profile_url: str, first_name: str, comment_text: str,
                             lead_magnet: dict, prefs: dict, post_id: int = None,
                             blog_url: str = "") -> "int | None":
    """Deliver the owned asset a commenter asked for by keyword — as an APPROVAL-GATED draft in the
    operator's existing scheduled_dms queue, never as a direct send (issue #624).

    Comment-gated delivery ("comment X and I'll send it") is the mechanic that makes an artifact CTA
    worth writing, but DM-ing every commenter at volume is a spam surface, so the payload goes
    through the same queue the #485 nurture drafts use: one open draft per thread, a per-day drafting
    cap, and a human approval before anything leaves. `send_scheduled_dm` then re-checks the user's
    max_dms_per_day at send time, so the cap is enforced on delivery too.

    Returns the scheduled_dms id, or None when nothing was queued. Best-effort and NON-FATAL — a
    delivery that can't be drafted must never break the reply sweep it rides on.
    """
    try:
        delivery = resolve_artifact_delivery(lead_magnet)
        if delivery["kind"] != ARTIFACT_KIND_LEAD_MAGNET or not delivery["deliverable"]:
            return None
        if delivery["keyword"].lower() not in (comment_text or "").lower():
            return None
        if not profile_url or has_received_lead_magnet(user_id, profile_url):
            return None
        # One open draft per person, across BOTH mechanics: a commenter who is already mid-nurture
        # must not also get an artifact draft stacked on the same thread.
        if (has_open_scheduled_dm(user_id, profile_url, source=SCHEDULED_DM_SOURCE_ARTIFACT)
                or has_open_scheduled_dm(user_id, profile_url, source=SCHEDULED_DM_SOURCE_NURTURE)):
            log_info(f"Artifact delivery: {first_name or profile_url} already has a queued draft; "
                     f"skipping", user_id=user_id, action_type="dm")
            return None
        cap = int((prefs or {}).get("max_dms_per_day") or 0)
        if count_scheduled_dms_created_today(user_id, source=SCHEDULED_DM_SOURCE_ARTIFACT) >= cap:
            log_info(f"Artifact delivery: daily draft cap ({cap}) reached", user_id=user_id,
                     action_type="dm")
            return None

        message = render_dm_placeholders(delivery["message"], first_name=(first_name or "").split(" ")[0],
                                         blog_url=blog_url or "")
        if not str(message or "").strip():
            log_warning("Artifact delivery: the lead-magnet message rendered empty — nothing to send",
                        user_id=user_id, post_id=post_id, action_type="dm")
            return None
        # Due now: the operator's approval is the only gate on timing, and a resource someone just
        # asked for is worthless a day late.
        due = datetime.now(timezone.utc).replace(tzinfo=None)
        dm_id = insert_scheduled_dm(user_id, profile_url, message, due,
                                    recipient_name=first_name or None,
                                    status=ScheduledDmStatus.PENDING,
                                    source=SCHEDULED_DM_SOURCE_ARTIFACT)
        if not dm_id:
            log_warning(f"Artifact delivery: drafted the lead magnet for {first_name or profile_url} "
                        f"but the scheduled_dms insert failed", user_id=user_id, action_type="dm")
            return None
        # Recorded on QUEUE, not on send: the row's job is to stop us drafting the same resource for
        # the same person on every sweep, and the draft already exists once it is in the queue.
        record_lead_magnet_sent(user_id, profile_url, post_id)
        log_info(f"Artifact delivery queued for approval to {first_name or profile_url} "
                 f"(keyword '{delivery['keyword']}')", user_id=user_id, post_id=post_id,
                 action_type="dm")
        return dm_id
    except Exception as e:
        log_warning("Artifact delivery could not be queued", exc=e, user_id=user_id, post_id=post_id,
                    action_type="dm")
        return None


def _reply_to_comments_on_open_post(driver, wait, user_id: int, post_id: int, my_profile,
                                    profile_synthesis: str) -> dict:
    """Navigate to the user's own post and reply to comments on it (thread-builder replies, plus
    reciprocity/lead-magnet handling). Shared by the per-post reply task and the recent-posts sweep.
    Returns a `_reply_outcome` dict (status + counts + human summary). Assumes the caller already
    has a live driver/profile.
    """
    post_url = get_post_url_from_log_for_user(user_id, post_id)
    if not post_url:
        log_info(f"No successful post URL for post {post_id}; skipping replies.")
        return _reply_outcome("no_post_url", "No post URL")
    # Ground replies in the canonical post body (posts table); fall back to the log message.
    post_message = get_post_content(post_id) or get_post_message_from_log_for_user(user_id, post_id)

    log_info("Replying to Comments of Post ID", post_id=post_id)
    if driver.current_url != post_url:
        driver.get(post_url)

    # SDUI: expand more replies where available, then collect comment items from the
    # new data-testid comment list (comments are no longer article.comments-comment-entity).
    # The miss IS this loop's exit condition, so it never warns (utilities/CLAUDE.md): a post whose
    # comments already fit on one page never renders the control, and one that does stops rendering
    # it once the last page is in — every sweep ends on a miss by design.
    for _ in range(5):
        more = click_first(driver, wait,
                           [(By.XPATH, "//button[contains(@aria-label,'more comment') or "
                                       "contains(normalize-space(),'Load more') or "
                                       "contains(normalize-space(),'more repl')]")],
                           "Load more comments", required=False, warn_on_miss=False,
                           user_id=user_id, post_id=post_id)
        if not more:
            break
        time.sleep(2)

    comments = _comment_items_from_thread(driver)
    log_info(f"Comments Found: {len(comments)}")

    # our profile slug — used to detect comments we AUTHORED or already replied to (the loop-breaker).
    our_slug = profile_slug(str(my_profile.profile_url))
    # LOOP SAFETY: without our slug we can't tell our own comments / already-replied ones apart, so a
    # sweep could reply to our own comments and re-reply every run. Fail SAFE — skip replying entirely.
    if not our_slug:
        log_warning("Reply sweep: could not resolve own profile slug — skipping replies to avoid "
                    "duplicate/self replies", user_id=user_id, post_id=post_id, action_type="reply")
        return _reply_outcome("no_profile_slug", "Skipped — no profile slug for dedup",
                              comments_found=len(comments))

    comments_replied_count = 0
    leads_flagged = 0
    prefs = get_engagement_preferences(user_id)
    lead_magnet = get_lead_magnet_settings(user_id)
    lead_magnet_blog_url = get_user_blog_url(user_id) if lead_magnet.get("enabled") else ""
    # Redis dedup TTL covers the configured look-back window plus one day so a comment that stays
    # alive across sweeps is only ever replied to once. Clamped to a sensible max.
    reply_dedup_ttl_days = min(14, max(1, int(prefs.get("reply_max_post_age_days") or 2) + 1))
    for comment in comments:
        # Per-post volume backstop: never fire an unbounded burst of replies from one sweep.
        if comments_replied_count >= _MAX_REPLIES_PER_SWEEP:
            log_info(f"Reply cap reached ({_MAX_REPLIES_PER_SWEEP}) for post {post_id}")
            break
        try:
            tb = comment.find_elements(By.CSS_SELECTOR, "[data-testid='expandable-text-box']")
            comment_text = ((tb[0].text if tb else comment.text) or "").strip()
        except Exception:
            continue
        short_comment_text = comment_text[:75]
        # Reciprocity + lead-magnet: read the commenter, record them as an engager, and
        # (if enabled) DM them the resource when their comment contains the trigger keyword.
        commenter_slug = ""
        try:
            _link = comment.find_element(By.CSS_SELECTOR, "a[href*='/in/']")
            # SDUI packs "Name Verified Profile 1st" into one link — keep the name, keep the badge
            # separately (it tells the connection scan who we're already connected to, issue #623).
            _eraw = (_link.text or "") or (_link.get_attribute("aria-label") or "")
            _ename = clean_person_name(_eraw)
            _edegree = connection_degree(_eraw)
            _eprofile = (_link.get_attribute("href") or "").split("?")[0]
            commenter_slug = profile_slug(_eprofile)
            # Never reply to a comment WE authored (seed, second-wave, or manual). It reads as the
            # user talking to themselves in the activity feed and stacks "responses" on their own post.
            if _href_is_profile(_eprofile, our_slug):
                log_debug("Skipping own comment", user_id=user_id, post_id=post_id, comment_text=short_comment_text)
                continue
            if _ename and _ename.lower() != (my_profile.full_name or "").lower():
                upsert_engager(user_id, _ename, _eprofile, connection_degree=_edegree)
                # Inbound intent (#483): this commenter may be raising their hand — flag + draft.
                if leads_flagged < _MAX_LEAD_FLAGS_PER_SWEEP and _flag_lead_signal(
                        user_id, comment_text, LeadSignalSource.POST_COMMENT, f"post:{post_id}",
                        person_name=_ename, person_profile_url=_eprofile,
                        channel=LeadSignalChannel.REPLY, post_id=post_id, context_url=post_url,
                        context_text=post_message, my_profile=my_profile, prefs=prefs,
                        profile_synthesis=profile_synthesis):
                    leads_flagged += 1
                # Comment-gated artifact delivery (#624): approval-gated draft, never a direct send.
                _queue_artifact_delivery(user_id, _eprofile, _ename, comment_text, lead_magnet,
                                         prefs, post_id=post_id, blog_url=lead_magnet_blog_url)
        except Exception:
            # A card without a readable /in/ link is routine SDUI variance; only the
            # reciprocity/lead capture is skipped — the reply flow below still proceeds.
            log_debug("Commenter read failed; skipping engager/lead capture",
                      user_id=user_id, post_id=post_id, comment_text=short_comment_text)
        # Already replied if our own profile link already appears in this comment's replies.
        # Use exact slug matching rather than a raw substring so a short slug does not match every
        # longer slug that contains it.
        already_replied = False
        if our_slug:
            try:
                for a in comment.find_elements(By.CSS_SELECTOR, "a[href*='/in/']"):
                    if _href_is_profile(a.get_attribute("href") or "", our_slug):
                        already_replied = True
                        break
            except Exception:
                already_replied = False
        # Cross-sweep backstop: even if the DOM does not show our previous reply (collapsed thread,
        # re-render, etc.), Redis remembers we already replied to this commenter+text on this post.
        if not already_replied and _has_replied_to_comment(user_id, post_id, commenter_slug, comment_text):
            log_debug("Already replied to this comment in a previous sweep", user_id=user_id, post_id=post_id, comment_text=short_comment_text)
            already_replied = True
        if already_replied:
            log_debug("Already replied to this comment", user_id=user_id, post_id=post_id, comment_text=short_comment_text)
            continue
        log_debug("Responding to this comment", user_id=user_id, post_id=post_id, comment_text=short_comment_text)
        # Thread-builder: reply in a way that ends with a follow-up question so the commenter
        # replies again — first-hour thread depth is the top 2026 reach signal.
        with llm_attribution(user_id=user_id, feature=FEATURE_COMMENT):
            response = generate_thread_reply(post_message, comment_text, my_profile,
                                             prefs=prefs,
                                             profile_synthesis=profile_synthesis)
        log_debug("AI Generated Response to Comment", user_id=user_id, post_id=post_id, response=response)
        if response and _reply_to_comment_inline(driver, wait, comment, response, user_id=user_id):
            insert_new_log(user_id=user_id, post_id=post_id, action_type=LogActionType.REPLY,
                           result=LogResultType.SUCCESS, post_url=post_url, message=response)
            record_action(user_id, ACTION_REPLY)  # tracked, but outside the outbound envelope (#626)
            comments_replied_count += 1
            _record_replied_to_comment(user_id, post_id, commenter_slug, comment_text,
                                       ttl_days=reply_dedup_ttl_days)
            time.sleep(random.uniform(5, 12))
        else:
            insert_new_log(user_id=user_id, post_id=post_id, action_type=LogActionType.REPLY,
                           result=LogResultType.FAILURE, post_url=post_url, message=response)
    return _reply_outcome("ok", f"Replied to {comments_replied_count} comments",
                          comments_found=len(comments), replies_sent=comments_replied_count)


def _retry_golden_hour_sweep(user_id: int, sweep_slot: int, attempt: int, status: str) -> bool:
    """A golden-hour sweep that could not run at all REPORTS itself and then gets one more chance
    while the window is still open — the #401 amplifier used to lose the whole hour to a single
    transient 429, and the audit could never tell that apart from a post nobody commented on.

    The report is what makes the difference visible: it carries `status` ("rate_limited",
    "session_failed") against the freshest post's latency, so a silent hour has a cause in PostHog
    instead of an absence. Returns True when a retry was scheduled — bounded by
    `sweep_retry_countdown` (attempts AND window), so a sustained rate-limit decays to nothing
    instead of hammering LinkedIn.
    """
    post_ids = get_recent_posted_post_ids(user_id, days=1)
    if not post_ids:
        return False
    # The report carries the latency reading the retry decision needs, so the post age is read once.
    report = _record_golden_hour_report(user_id, post_ids[0], sweep_slot,
                                        _reply_outcome(status, f"Sweep could not run ({status})"))
    countdown = _golden.sweep_retry_countdown(report.get("latency_minutes") if report else None,
                                              attempt)
    if countdown is None:
        return False
    sweep_reply_comments.apply_async(
        kwargs={'user_id': user_id, 'sweep_slot': sweep_slot, 'attempt': int(attempt) + 1},
        countdown=countdown)
    log_info(f"Golden-hour sweep retry {int(attempt) + 1} scheduled in {countdown}s ({status})",
             user_id=user_id, action_type="reply", task_name="sweep_reply_comments")
    return True


@shared_task.task(name='cqc_lem.app.run_automation.sweep_reply_comments',
                  bind=True, base=QueueOnce,
                  once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id', 'sweep_slot']},
                  queue='se_engage')
def sweep_reply_comments(self, user_id: int, sweep_slot: int = 0, attempt: int = 0):
    """Reply to new comments across the user's RECENT posts in ONE Selenium session. Triggered by a
    forwarded comment-notification email (event mode) or the scheduled dispatcher — replacing the old
    24h-per-post polling loop that drove the 429 rate-limiting. 429-safe: a rate-limited session logs
    a clean skip and returns (a later trigger/sweep retries). sweep_slot is part of the QueueOnce key
    so the golden-hour amplifier can enqueue several distinct sweeps for one user (same user_id+slot
    still dedups); the single-shot scheduled/API triggers leave it at 0.

    Every post swept emits a golden-hour report (issue #622) — comments found, replies sent, minutes
    since publish — so the amplifier's silence can be diagnosed instead of guessed at. `attempt` is
    the in-window retry counter; it is NOT part of the QueueOnce key, so a retry of the same slot
    still dedups against a concurrently-queued one.
    """
    prefs = get_engagement_preferences(user_id)
    days = int(prefs.get("reply_max_post_age_days") or 2)
    post_ids = get_recent_posted_post_ids(user_id, days=days)
    if not post_ids:
        return "No recent posts to sweep"
    # Single-flight: QueueOnce uses unlock_before_run (frees the lock at task START to avoid a
    # crashed task holding it forever), which leaves a narrow window where the email-event trigger
    # and the scheduled dispatcher could start two concurrent sweeps and double-reply. This lock
    # closes that window; it fails OPEN if Redis is down. (Belt-and-suspenders with #474.)
    lock_name = f"sweep_reply_comments:{user_id}"
    lock_token = acquire_run_lock(lock_name, ttl_seconds=1800)
    if lock_token is None:
        log_info(f"Another reply sweep is already running for user {user_id} — skipping.")
        return "Skipped — another reply sweep in progress"
    try:
        driver, wait, _user_email, my_profile = get_current_profile(user_id=user_id, session_name="Reply Sweep")
    except LinkedInRateLimited as e:
        log_warning("Reply sweep skipped — LinkedIn rate-limited", exc=e, user_id=user_id,
                    task_name="sweep_reply_comments")
        release_run_lock(lock_name, lock_token)
        retried = _retry_golden_hour_sweep(user_id, sweep_slot, attempt, "rate_limited")
        return "Skipped — rate limited" + (" (retry scheduled)" if retried else "")
    except Exception as e:
        log_error("Error starting reply sweep", exc=e, user_id=user_id, task_name="sweep_reply_comments")
        release_run_lock(lock_name, lock_token)
        retried = _retry_golden_hour_sweep(user_id, sweep_slot, attempt, "session_failed")
        return f"Failed to start reply sweep: {e}" + (" (retry scheduled)" if retried else "")
    try:
        profile_synthesis = get_or_create_profile_synthesis(user_id, my_profile)
        swept = 0
        for post_id in post_ids:
            try:
                outcome = _reply_to_comments_on_open_post(driver, wait, user_id, post_id, my_profile,
                                                          profile_synthesis)
                swept += 1
            except Exception as e:
                log_warning("Reply sweep: post failed", exc=e, user_id=user_id, post_id=post_id,
                            task_name="sweep_reply_comments")
                outcome = _reply_outcome("error", f"Reply sweep failed: {e}")
            _record_golden_hour_report(user_id, post_id, sweep_slot, outcome)
        return f"Swept replies on {swept}/{len(post_ids)} recent posts"
    finally:
        quit_gracefully(driver)
        release_run_lock(lock_name, lock_token)


# --- follow-up on replies to OUR automated comments on OTHERS' posts (issue #478) --------------
# When someone (usually the author) replies to a comment WE automated, we react to their reply and,
# if it asks a question, post a voice-aligned answer. Scoped to OUR automated comments ONLY: the
# posts come from the commented_posts ledger (which only holds comments comment_on_feed_inline made
# — never anything the user typed by hand), so a manual comment is structurally out of scope.
_FOLLOWUP_WINDOW_DAYS = 3           # only revisit posts we commented on this recently
_MAX_FOLLOWUP_REACTS_PER_RUN = 25   # volume backstop per post-sweep
_MAX_FOLLOWUP_REPLIES_PER_DAY = 10  # cap on auto-replies/day (a real DM-like touch)


def _post_url_from_key(key: str) -> "str | None":
    """A navigable LinkedIn post URL from a ledger key. feedurn://urn:li:activity:<id> ->
    https://www.linkedin.com/feed/update/urn:li:activity:<id>/. Returns None for the legacy
    feedpost:// hash keys (not navigable) or anything unrecognized.
    """
    if not key:
        return None
    key = str(key)
    if key.startswith("feedurn://"):
        urn = key[len("feedurn://"):]
        return f"https://www.linkedin.com/feed/update/{urn}/" if urn.startswith("urn:li:") else None
    if key.startswith("http"):
        return key
    return None


def _reply_is_question(text: str) -> bool:
    """True if a reply is asking us something (worth an auto-reply). A literal '?' is the primary
    signal; strip URLs first so a link's query string never counts. Reactions are handled
    separately — this only gates whether we also post a reply.
    """
    if not text:
        return False
    stripped = re.sub(r"https?://\S+", " ", text)
    return "?" in stripped


def _followup_reply_key(post_key: str, replier_href: str, reply_text: str) -> str:
    """Stable dedup id for a specific reply so we react/reply to it AT MOST ONCE (the #474 lesson —
    key on identity, not raw text). Anchored to the post, the replier's profile slug, and a
    NORMALIZED hash of the reply text.
    """
    slug = ""
    if replier_href:
        m = re.search(r"/in/([^/?#]+)", replier_href)
        slug = (m.group(1).lower() if m else "")
    norm = _normalize_post_text(reply_text)[:200]
    digest = hashlib.sha1(f"{slug}|{norm}".encode("utf-8", "ignore")).hexdigest()[:16]
    return f"{post_key}#reply:{slug}:{digest}"


def _react_to_comment_inline(driver, wait, comment_el, user_id: int = None) -> bool:
    """Like a comment/reply (best-effort, non-fatal). The action bar is HOVER-HIDDEN (the react
    button is zero-size until the comment is hovered), so hover first, then click the react control
    ('Open reactions menu' on this SDUI; a single click applies the default Like). If the click
    opens the reaction flyout instead, pick the visible 'Like'. Skips if already reacted.
    """
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", comment_el)
        try:
            ActionChains(driver).move_to_element(comment_el).pause(0.7).perform()  # reveal the bar
            time.sleep(random.uniform(0.5, 1.0))
        except Exception:
            pass  # hover is best-effort; a headless/again-stale element still gets the click below
        btns = comment_el.find_elements(
            By.CSS_SELECTOR,
            "button[aria-label^='React '], button[aria-label='Like'], "
            "button[aria-label='Open reactions menu'], button[aria-label*='Like']")
        for b in btns:
            if (b.get_attribute("aria-pressed") or "").lower() == "true":
                return False  # already liked
            try:
                sz = b.size or {}
                if sz.get("width", 0) > 0 and sz.get("height", 0) > 0:
                    ActionChains(driver).move_to_element(b).pause(0.3).click(b).perform()
                else:  # still zero-size after hover — bypass interactability
                    driver.execute_script("arguments[0].click();", b)
            except Exception:
                driver.execute_script("arguments[0].click();", b)
            time.sleep(random.uniform(1, 2))
            # If a reaction flyout opened rather than applying Like, click the visible Like option.
            if (b.get_attribute("aria-pressed") or "").lower() != "true":
                for lk in driver.find_elements(
                        By.CSS_SELECTOR, "button[aria-label='Like'], button[aria-label^='React Like']"):
                    try:
                        if lk.is_displayed() and (lk.size or {}).get("width", 0) > 0:
                            ActionChains(driver).move_to_element(lk).pause(0.2).click(lk).perform()
                            time.sleep(random.uniform(0.8, 1.4)); break
                    except Exception:
                        continue
            return True
        labels = [b.get_attribute("aria-label") for b in comment_el.find_elements(By.CSS_SELECTOR, "button")]
        log_warning(f"No like button matched on reply; buttons={[l for l in labels if l][:8]}",
                    action_type="engaged", user_id=user_id)
        return False
    except Exception as e:
        log_warning("Could not react to comment reply", exc=e, action_type="engaged", user_id=user_id)
        return False


def _load_comment_thread(driver) -> None:
    """Make a post's comment thread actually render: a TALL viewport is what lazy-renders comments a
    long post pushes far below the fold (scrolling alone on the default 1080-tall window did not —
    validated live, issue #478), then scroll down and expand the '…more' / 'previous replies'
    controls. Best-effort throughout; every step is optional.
    """
    try:
        driver.set_window_size(1400, 3400)
    except Exception:
        pass  # some drivers reject resize; the scrolling below is the fallback
    for _ in range(10):
        driver.execute_script("window.scrollBy(0, 1000);")
        time.sleep(random.uniform(0.9, 1.4))
        cl = driver.find_elements(By.CSS_SELECTOR, "[data-testid*='commentList']")
        if cl:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", cl[0])
    for _ in range(6):
        exp = [b for b in driver.find_elements(By.CSS_SELECTOR, "[data-testid*='commentList'] button")
               if re.search(r'\bmore\b|repl', (b.text or ''), re.I)]
        if not exp:
            break
        try:
            driver.execute_script("arguments[0].click();", exp[0]); time.sleep(random.uniform(1.2, 2))
        except Exception:
            break


def _followup_on_post_comment_replies(driver, wait, user_id: int, post_url: str, post_key: str,
                                      my_profile, profile_synthesis: str, prefs: dict,
                                      replies_remaining: int) -> dict:
    """Revisit ONE post we commented on: react to replies to our comment, answer question-replies,
    and flag buying intent. Returns {'reacted': n, 'replied': n, 'leads': n}. Best-effort/non-fatal
    (issues #478, #483).
    """
    result = {"reacted": 0, "replied": 0, "leads": 0}
    path = urlparse(str(my_profile.profile_url)).path
    our_slug = path.split("/")[2] if len(path.split("/")) > 2 else None
    if not our_slug:
        log_warning("Follow-up: no profile slug — skipping to avoid mis-scoping", user_id=user_id,
                    action_type="reply")
        return result
    if driver.current_url.split("?")[0].rstrip("/") != post_url.split("?")[0].rstrip("/"):
        driver.get(post_url)
        time.sleep(random.uniform(2.5, 4))
    _load_comment_thread(driver)

    items = _comment_items(driver)
    our_conts = [c for (_tb, c, a) in items if f"/in/{our_slug}" in a]
    log_info(f"Follow-up: {len(items)} comment box(es), {len(our_conts)} ours, on {post_url}",
             user_id=user_id, task_name="sweep_comment_followups")

    for tb, cont, author in items:
        if result["reacted"] >= _MAX_FOLLOWUP_REACTS_PER_RUN:
            break
        if not author or f"/in/{our_slug}" in author:
            continue  # skip our own comments/replies
        # A reply is "to our comment" if its block is nested in our comment's thread OR its body
        # @mentions us — LinkedIn auto-prepends the @mention of the person being replied to, so a
        # reply to us reliably carries our /in/ link inside its text box.
        nested = any(driver.execute_script("return arguments[0].contains(arguments[1]);", oc, cont)
                     for oc in our_conts)
        try:
            mentions_us = bool(tb.find_elements(By.CSS_SELECTOR, f"a[href*='/in/{our_slug}']"))
        except Exception:
            mentions_us = False
        if not (nested or mentions_us):
            continue
        try:
            reply_text = (tb.text or "").strip()
        except Exception:
            continue
        if not reply_text:
            continue
        # Inbound intent (#483): a reply to our comment is a prime place for "can you help with X?".
        if result["leads"] < _MAX_LEAD_FLAGS_PER_SWEEP and _flag_lead_signal(
                user_id, reply_text, LeadSignalSource.COMMENT_REPLY, post_key,
                person_profile_url=author, channel=LeadSignalChannel.REPLY, context_url=post_url,
                my_profile=my_profile, prefs=prefs, profile_synthesis=profile_synthesis):
            result["leads"] += 1
        reply_key = _followup_reply_key(post_key, author, reply_text)
        state = get_comment_followup(user_id, reply_key) or {}
        did_react = bool(state.get("reacted"))
        did_reply = bool(state.get("replied"))
        if not did_react and _react_to_comment_inline(driver, wait, cont, user_id=user_id):
            result["reacted"] += 1
            did_react = True
            record_comment_followup(user_id, post_key, reply_key, reacted=True)
            insert_new_log(user_id=user_id, action_type=LogActionType.ENGAGED,
                           result=LogResultType.SUCCESS, post_url=post_url,
                           message="Reacted to reply on our comment")
        if not did_reply and replies_remaining > 0 and _reply_is_question(reply_text):
            # We are a GUEST replying in someone else's thread — not the post author (issue #478).
            with llm_attribution(user_id=user_id, feature=FEATURE_COMMENT):
                response = generate_comment_reply_followup(reply_text, my_profile, prefs=prefs,
                                                           profile_synthesis=profile_synthesis)
            if response and _reply_under_comment_inline(driver, wait, cont, response, user_id=user_id):
                result["replied"] += 1
                replies_remaining -= 1
                record_comment_followup(user_id, post_key, reply_key, reacted=did_react, replied=True)
                insert_new_log(user_id=user_id, action_type=LogActionType.REPLY,
                               result=LogResultType.SUCCESS, post_url=post_url, message=response)
                record_action(user_id, ACTION_REPLY)  # tracked, outside the outbound envelope (#626)
                time.sleep(random.uniform(5, 12))
    return result


@shared_task.task(name='cqc_lem.app.run_automation.sweep_comment_followups',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_engage')
def sweep_comment_followups(self, user_id: int):
    """Revisit posts we automated a comment on in the last few days and follow up on replies to our
    comment: react to each reply, and answer question-replies (issue #478). Only touches OUR
    automated comments (the commented_posts ledger). QueueOnce + single-flight lock + 429-safe.
    """
    return _run_comment_followups_sweep(user_id)


def _run_comment_followups_sweep(user_id: int) -> str:
    """Body of sweep_comment_followups, extracted so it is unit-testable without the QueueOnce/Redis
    task wrapper.
    """
    posts = get_recent_navigable_commented_posts(user_id, days=_FOLLOWUP_WINDOW_DAYS)
    if not posts:
        return "No recent navigable commented posts to follow up on"
    lock_name = f"sweep_comment_followups:{user_id}"
    lock_token = acquire_run_lock(lock_name, ttl_seconds=1800)
    if lock_token is None:
        return "Skipped — another follow-up sweep in progress"
    prefs = get_engagement_preferences(user_id)
    replies_remaining = max(0, _MAX_FOLLOWUP_REPLIES_PER_DAY - count_followup_replies_today(user_id))
    try:
        driver, wait, _email, my_profile = get_current_profile(user_id=user_id, session_name="Comment Follow-ups")
    except LinkedInRateLimited as e:
        log_warning("Follow-up sweep skipped — rate-limited", exc=e, user_id=user_id,
                    task_name="sweep_comment_followups")
        release_run_lock(lock_name, lock_token)
        return "Skipped — rate limited"
    except Exception as e:
        log_error("Error starting follow-up sweep", exc=e, user_id=user_id, task_name="sweep_comment_followups")
        release_run_lock(lock_name, lock_token)
        return f"Failed to start follow-up sweep: {e}"
    try:
        synthesis = get_or_create_profile_synthesis(user_id, my_profile)
        reacted = replied = leads = 0
        for row in posts:
            key = row.get("post_key")
            url = _post_url_from_key(key)
            if not url:
                continue
            try:
                r = _followup_on_post_comment_replies(driver, wait, user_id, url, key, my_profile,
                                                      synthesis, prefs, replies_remaining)
                reacted += r["reacted"]; replied += r["replied"]; replies_remaining -= r["replied"]
                leads += r.get("leads", 0)
            except Exception as e:
                log_warning("Follow-up: post failed", exc=e, user_id=user_id,
                            task_name="sweep_comment_followups")
        return (f"Follow-ups: reacted {reacted}, replied {replied}, leads {leads} "
                f"across {len(posts)} post(s)")
    finally:
        quit_gracefully(driver)
        release_run_lock(lock_name, lock_token)


@shared_task.task(name='cqc_lem.app.run_automation.process_comment_followups_for_url',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id', 'post_url']},
                  queue='se_engage')
def process_comment_followups_for_url(self, user_id: int, post_url: str):
    """Single-post entrypoint for the follow-up feature — run it against ONE post URL (manual/API/
    verification), independent of the ledger. Reacts to replies on our comment and answers
    questions, same as the sweep (issue #478).
    """
    return _run_single_post_followup(user_id, post_url)


def _run_single_post_followup(user_id: int, post_url: str) -> str:
    """Body of process_comment_followups_for_url, extracted for unit testing (no QueueOnce/Redis)."""
    key = None
    m = re.search(r"urn:li:(?:activity|ugcPost|share):\d+", post_url or "")
    if m:
        key = f"feedurn://{m.group(0).lower()}"
    if not key:
        return "No activity URN in the given URL"
    lock_name = f"sweep_comment_followups:{user_id}"
    lock_token = acquire_run_lock(lock_name, ttl_seconds=1800)
    if lock_token is None:
        return "Skipped — another follow-up run in progress"
    prefs = get_engagement_preferences(user_id)
    replies_remaining = max(0, _MAX_FOLLOWUP_REPLIES_PER_DAY - count_followup_replies_today(user_id))
    try:
        driver, wait, _email, my_profile = get_current_profile(user_id=user_id, session_name="Comment Follow-up (single)")
    except Exception as e:
        log_error("Error starting single follow-up", exc=e, user_id=user_id,
                  task_name="process_comment_followups_for_url")
        release_run_lock(lock_name, lock_token)
        return f"Failed: {e}"
    try:
        synthesis = get_or_create_profile_synthesis(user_id, my_profile)
        url = _post_url_from_key(key)
        r = _followup_on_post_comment_replies(driver, wait, user_id, url, key, my_profile,
                                              synthesis, prefs, replies_remaining)
        return (f"Follow-up on {url}: reacted {r['reacted']}, replied {r['replied']}, "
                f"leads {r.get('leads', 0)}")
    finally:
        quit_gracefully(driver)
        release_run_lock(lock_name, lock_token)


def _scrape_activity_comment_urns(driver, wait, my_profile) -> dict:
    """Map {normalized comment text -> post URL} from the user's own recent-activity/comments page.
    Each activity card holds the post's /feed/update/ permalink plus the comment we left, so we can
    recover the navigable URN for comments the ledger only has a hash for. Best-effort; validated on
    a supervised run (issue #478).
    """
    mapping = {}
    path = urlparse(str(my_profile.profile_url)).path.rstrip("/")
    if not path:
        return mapping
    driver.get(f"https://www.linkedin.com{path}/recent-activity/comments/")
    time.sleep(random.uniform(3, 5))
    for _ in range(8):
        driver.execute_script("window.scrollBy(0, 1400);")
        time.sleep(random.uniform(1.5, 2.5))
    for box in driver.find_elements(By.CSS_SELECTOR, _FEED_POST_TEXT_SEL):
        try:
            text = (box.text or "").strip()
            if len(text) < 15:
                continue
            card = _card_for_textbox(driver, box) or box
            url = _post_permalink_from_card(card)
            if not url:
                urn = _feed_post_urn_from_card(card, driver=driver)
                url = f"https://www.linkedin.com/feed/update/{urn}/" if urn else None
            if url:
                mapping[_normalize_post_text(text)[:200]] = url
        except Exception:
            continue
    return mapping


@shared_task.task(name='cqc_lem.app.run_automation.reconcile_recent_comment_urns',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_engage')
def reconcile_recent_comment_urns(self, user_id: int, days: int = _FOLLOWUP_WINDOW_DAYS):
    """Backfill: recover navigable URNs for recent 'feedpost://' ledger rows via the user's own
    recent-activity/comments page, so pre-#474 comments become follow-up-able. Matches each activity
    comment to a ledger row by our comment text, then upgrades the key to feedurn:// (issue #478).
    """
    return _run_reconcile_comment_urns(user_id, days)


def _run_reconcile_comment_urns(user_id: int, days: int = _FOLLOWUP_WINDOW_DAYS) -> str:
    """Body of reconcile_recent_comment_urns, extracted for unit testing (no QueueOnce/Redis)."""
    stale = [r for r in get_recent_commented_rows_with_text(user_id, days=days)
             if str(r.get("post_key", "")).startswith("feedpost://")]
    if not stale:
        return "No stale (feedpost://) commented posts in window"
    lock_name = f"reconcile_comment_urns:{user_id}"
    lock_token = acquire_run_lock(lock_name, ttl_seconds=1200)
    if lock_token is None:
        return "Skipped — reconcile already running"
    try:
        driver, wait, _email, my_profile = get_current_profile(user_id=user_id, session_name="Reconcile Comment URNs")
    except Exception as e:
        log_error("Error starting reconcile", exc=e, user_id=user_id, task_name="reconcile_recent_comment_urns")
        release_run_lock(lock_name, lock_token)
        return f"Failed: {e}"
    try:
        mapping = _scrape_activity_comment_urns(driver, wait, my_profile)  # {our_comment_text_norm: post_url}
        upgraded = 0
        for row in stale:
            old_key = row["post_key"]
            body = (row.get("comment_text") or "").strip()
            url = mapping.get(_normalize_post_text(body)[:200]) if body else None
            m = re.search(r"urn:li:(?:activity|ugcPost|share):\d+", url or "")
            if not m:
                continue
            new_key = f"feedurn://{m.group(0).lower()}"
            if update_commented_post_key(user_id, old_key, new_key):
                upgraded += 1
        return f"Reconciled {upgraded}/{len(stale)} stale commented-post keys"
    finally:
        quit_gracefully(driver)
        release_run_lock(lock_name, lock_token)


# --- Comment outcome tracking (issue #628) -------------------------------------------------
# LEM posted comments and never looked back. LinkedIn's May 2026 enforcement demotes
# automated-looking comments out of the default 'Most relevant' comment view — a SILENT kill that
# makes the whole commenting effort worthless while every log still says "success". This sweep
# revisits each posted comment once at T+24h and records what it actually earned: author replies,
# likes, replies in the thread, and whether it is still visible under the default sort.
_OUTCOME_MIN_AGE_HOURS = 24    # give the thread a day to earn a reply before judging it
_OUTCOME_MAX_AGE_HOURS = 168   # a sweep missed for days still records the sample (never re-checked)
_MAX_OUTCOME_CHECKS_PER_RUN = 15  # volume backstop — one post navigation each

# LinkedIn's comment sort control renders as a button carrying the CURRENT sort ("Most relevant" /
# "Most recent"). The default is 'Most relevant', which is the view the demotion signal is about.
_SORT_MOST_RELEVANT = "most relevant"
_SORT_MOST_RECENT = "most recent"

# The case fold (`_X_LOWER_TEXT` / `_X_LOWER_ARIA`) is shared with the feed sort control above:
# LinkedIn renders 'Most recent', and a literal case-sensitive match against any other casing
# silently never fires — which would leave the sort flip permanently failing and the demotion
# signal permanently NULL.
# XPath's normalize-space() is the WHOLE SUBTREE's text, so an unbounded contains() on a generic
# element also matches every ancestor wrapper up to <body> — and find_first returns the first match
# in document order, i.e. the outermost one. That element's .text is then the whole page, so the
# sort would be decided by any comment that happens to say 'most recent', and clicking it would
# never open the real control (a click on a wrapper does not activate the button inside it). Any
# locator that is not already restricted to a <button> carries this bound.
_X_SHORT_TEXT = "string-length(normalize-space()) < 40"
_COMMENT_SORT_LOCATORS = [
    # LinkedIn's SDUI uses data-testid for stable identity; prefer that over text/aria-label drift.
    # The KNOWN testid leads; the wildcard behind it requires both substrings, since a bare *='sort'
    # would also claim an unrelated sort control on the page.
    (By.CSS_SELECTOR, "[data-testid='comment-sort-dropdown']"),
    (By.CSS_SELECTOR, "button[data-testid*='sort'][data-testid*='comment']"),
    # Older / fallback: any button whose text or aria-label names the current sort.
    (By.XPATH, f"//button[contains({_X_LOWER_ARIA},'sort') and "
               f"(contains({_X_LOWER_TEXT},'{_SORT_MOST_RELEVANT}') or "
               f"contains({_X_LOWER_TEXT},'{_SORT_MOST_RECENT}'))]"),
    (By.XPATH, f"//button[{_X_LOWER_TEXT}='{_SORT_MOST_RELEVANT}' or "
               f"{_X_LOWER_TEXT}='{_SORT_MOST_RECENT}']"),
    (By.XPATH, f"//button[contains({_X_LOWER_TEXT},'{_SORT_MOST_RELEVANT}') or "
               f"contains({_X_LOWER_TEXT},'{_SORT_MOST_RECENT}')]"),
    # Some renders surface the control as a non-button interactive element (role=button) or plain
    # labeled div. The label extractor below still decides whether it really is the sort control,
    # so these stay broad — but bounded to a node whose OWN text is the label (see _X_SHORT_TEXT).
    (By.XPATH, f"//*[self::button or @role='button'][contains({_X_LOWER_ARIA},'sort')]"),
    (By.XPATH, f"//*[self::button or @role='button'][{_X_SHORT_TEXT}]"
               f"[contains({_X_LOWER_TEXT},'{_SORT_MOST_RELEVANT}') or "
               f"contains({_X_LOWER_TEXT},'{_SORT_MOST_RECENT}')]"),
    # Innermost div only: a click bubbles UP to whatever handler owns the control, so the deepest
    # labeled node is both the right label to read and the right node to click.
    (By.XPATH, f"//div[not(.//div)][{_X_SHORT_TEXT}]"
               f"[contains({_X_LOWER_TEXT},'{_SORT_MOST_RELEVANT}') or "
               f"contains({_X_LOWER_TEXT},'{_SORT_MOST_RECENT}')]"),
]


def _sort_option_locators(target: str) -> list:
    """Menu-option locators for ONE sort, compared case-insensitively against the lowercase target.
    An exact-case literal ('Most Recent') never matches LinkedIn's 'Most recent', and a sort flip
    that can never find its option makes every absent comment read as unfindable instead of
    demoted — the one reading this feature exists to produce.
    """
    target = (target or "").strip().lower()
    return [
        (By.XPATH, "//*[self::button or @role='menuitem' or @role='menuitemradio']"
                   f"[{_X_LOWER_TEXT}='{target}']"),
        (By.XPATH, f"//*[{_X_LOWER_TEXT}='{target}']"),
    ]

# A rendered comment is truncated behind '…more' until expanded, so our comment is matched on a
# truncation-proof normalized PREFIX (the #474 lesson applied to comment bodies), never full text.
_COMMENT_MATCH_PREFIX_CHARS = 60

# The reaction count sits on/next to the comment's own reactions control. Document order is what
# scopes it: our comment's control precedes the nested replies inside the same container.
_COMMENT_LIKE_COUNT_JS = (
    "const c=arguments[0];"
    "for(const el of c.querySelectorAll('button,span,a')){"
    "  const t=((el.getAttribute('aria-label')||'')+' '+(el.innerText||'')).trim();"
    "  const m=t.match(/([0-9][0-9.,]*[KMkm]?)\\s*(?:reaction|like)/i);"
    "  if(m) return m[1];"
    "}return '';")


# Reading a candidate's label costs two Selenium round-trips, and the broad tail of the chain can
# match many nodes on a busy thread. Only the first few per locator are worth checking.
_SORT_CANDIDATE_SCAN_CAP = 8

# When the sort control cannot be read on a page that DID render comments, capture the
# sort-control-like candidates so the next locator iteration has fresh evidence. The scan is
# bounded: it looks only at interactive-ish elements near the comment list and keeps the first
# `_SORT_CANDIDATE_SCAN_CAP` hits. Purely read-only / DEBUG — it never changes the outcome.
_SORT_CONTROL_DIAGNOSTIC_JS = (
    "const root=document.querySelector(\"[data-testid*='commentList']\")||document.body;"
    "const out=[];"
    "for(const el of root.querySelectorAll('button,[role=\"button\"],select,[aria-haspopup],div')){"
    "  const aria=(el.getAttribute('aria-label')||'');"
    "  const text=(el.innerText||'');"
    "  const blob=(aria+' '+text+' '+(el.getAttribute('data-testid')||'')+' '+(el.getAttribute('class')||'')).toLowerCase();"
    "  if(/sort|most relevant|most recent|top|newest/.test(blob)){"
    "    out.push({"
    "      tag:el.tagName.toLowerCase(),"
    "      data_testid:el.getAttribute('data-testid')||'',"
    "      aria_label:aria.slice(0,120),"
    "      role:el.getAttribute('role')||'',"
    "      text:text.replace(/\\s+/g,' ').trim().slice(0,80),"
    "      has_popup:el.getAttribute('aria-haspopup')||'',"
    "      classes:(el.getAttribute('class')||'').split(/\\s+/).filter(c=>c.length>3).slice(0,6).join(' ')"
    "    });"
    "  }"
    "  if(out.length>=8) break;"
    "}"
    "return out;")


def _sort_from_element(el) -> str:
    """The sort an element names ('most relevant' / 'most recent'), or '' when it names neither —
    which is how a wrong-but-matching node is told apart from the real control.
    """
    try:
        text = f"{el.get_attribute('aria-label') or ''} {el.text or ''}".lower()
    except Exception:
        return ""
    if _SORT_MOST_RECENT in text:
        return _SORT_MOST_RECENT
    if _SORT_MOST_RELEVANT in text:
        return _SORT_MOST_RELEVANT
    return ""


def _diagnose_sort_control_miss(driver) -> list[dict]:
    """Candidate elements near the comment list that look sort-control-ish, for DEBUG evidence.

    Called only when a rendered thread yields no readable sort control. The scan is bounded and
    read-only; it returns structured descriptors (tag, data-testid, aria-label, role, text) so the
    next locator iteration can be written against production evidence rather than guesses.
    """
    try:
        result = driver.execute_script(_SORT_CONTROL_DIAGNOSTIC_JS)
        return [dict(r) for r in (result or []) if isinstance(r, dict)]
    except Exception:
        return []


def _find_comment_sort_control(driver, wait, *, warn_on_miss: bool = True):
    """The comment sort control, preferring a candidate whose own label actually reads as a sort.

    find_first hands back the first match of the first locator that yields ANY element — it never
    looks at what it found. One unrelated 'sort' button matched by a broad fallback would therefore
    be returned forever, read as unreadable, and never warn ('Selector miss' only fires on a TOTAL
    miss), which is exactly the starved denominator #818 is about. Walking the chain here lets such
    a node fall through to a locator that names the real sort. Some renders label the control only
    inside its popup, so an unvalidated candidate is still returned when nothing in the chain parses
    — that is the pre-existing behaviour, and the click path in `_switch_comment_sort` needs it.

    `warn_on_miss` is the caller's page-native cross-check (#1063): a total miss is only selector
    rot on a page that rendered a comment thread at all.
    """
    fallback = None
    for locator in _COMMENT_SORT_LOCATORS:
        try:
            els = driver.find_elements(*locator)[:_SORT_CANDIDATE_SCAN_CAP]
        except Exception:
            continue
        for el in els:
            if _sort_from_element(el):
                return el
            if fallback is None:
                fallback = el
    if fallback is not None:
        return fallback
    # Nothing at all yet: fall back to find_first for its wait/retry and its Selector-miss warning.
    return find_first(driver, wait, _COMMENT_SORT_LOCATORS, "Comment sort control", required=False,
                      warn_on_miss=warn_on_miss)


def _comment_sort_label(driver, wait, *, warn_on_miss: bool = True) -> str:
    """The comment sort currently applied, lowercased ('most relevant' / 'most recent'), or '' when
    the control isn't present. '' is load-bearing: without knowing the sort we cannot say whether an
    absent comment was demoted, so the visibility reading stays NULL rather than guessing.
    """
    try:
        btn = _find_comment_sort_control(driver, wait, warn_on_miss=warn_on_miss)
    except Exception:
        return ""
    return _sort_from_element(btn) if btn is not None else ""


def _switch_comment_sort(driver, wait, target: str = _SORT_MOST_RECENT) -> bool:
    """Best-effort flip of the comment sort. True only when the control afterwards reports `target`
    — an unverified flip would let 'not found here' be read as a demotion when the sort never
    actually changed.
    """
    try:
        btn = _find_comment_sort_control(driver, wait)
        if btn is None:
            return False
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(random.uniform(1, 2))
        opt = find_first(driver, wait, _sort_option_locators(target), f"{target} sort option",
                         required=False)
        if opt is None:
            return False
        driver.execute_script("arguments[0].click();", opt)
        time.sleep(random.uniform(2, 3.5))
        return _comment_sort_label(driver, wait) == target
    except Exception as e:
        log_warning("Comment sort switch failed", exc=e, action_type="scrape")
        return False


def _comment_text_matches(rendered: str, logged: str) -> bool:
    """True when a rendered comment box is the comment we logged. Compares truncation-proof
    normalized prefixes in BOTH directions, so a '…more'-collapsed render still matches the full
    text we stored. Empty on either side never matches — two blank reads must not collide.
    """
    a = _norm_prefix(rendered, _COMMENT_MATCH_PREFIX_CHARS)
    b = _norm_prefix(logged, _COMMENT_MATCH_PREFIX_CHARS)
    if not a or not b:
        return False
    return a.startswith(b) or b.startswith(a)


def _find_our_comment(items: list, our_slug: str, comment_text: str):
    """Our comment's container within a rendered thread, or None.

    Falls back to the single container authored by us when the text doesn't match: the
    commented_posts ledger makes our automated commenting at-most-once per post, so one comment of
    ours on the post IS this comment even when an @mention or emoji re-render broke the prefix
    match. With several of ours present (a manual comment, a reply) the text has to decide.
    """
    if not our_slug:
        return None
    ours = [(tb, cont) for (tb, cont, author) in items if _href_is_profile(author, our_slug)]
    if not ours:
        return None
    for tb, cont in ours:
        try:
            text = tb.text or ""
        except Exception:
            continue
        if _comment_text_matches(text, comment_text):
            return cont
    return ours[0][1] if len(ours) == 1 else None


def _comment_like_count(driver, container) -> int:
    """Reactions on one comment (0 when none/unreadable)."""
    try:
        return _parse_count(driver.execute_script(_COMMENT_LIKE_COUNT_JS, container) or "")
    except Exception:
        return 0


def _thread_replies(driver, our_cont, items: list) -> list:
    """[(container, author_href)] for the replies nested UNDER our comment. Replies are DOM-nested
    inside their parent comment's container (#478 thread map), and `contains` is true for the node
    itself, so our own container is excluded explicitly.
    """
    out = []
    for _tb, cont, author in items:
        try:
            nested = driver.execute_script(
                "return arguments[0]!==arguments[1] && arguments[0].contains(arguments[1]);",
                our_cont, cont)
        except Exception:
            continue
        if nested:
            out.append((cont, author or ""))
    return out


def _post_author_href(driver) -> str:
    """The post author's profile href on a post permalink page — the first /in/ link under <main>
    that is NOT inside the comment list (a commenter's link would otherwise win).
    """
    try:
        return driver.execute_script(
            "const root=document.querySelector('main')||document.body;"
            "for(const a of root.querySelectorAll(\"a[href*='/in/']\")){"
            "  if(!a.closest(\"[data-testid*='commentList']\")) return (a.href||'').split('?')[0];"
            "}return '';") or ""
    except Exception:
        return ""


def _read_comment_outcome(driver, wait, user_id: int, post_url: str, our_slug: str,
                          comment_text: str) -> dict:
    """Revisit ONE post and read what our comment there actually earned (issue #628).

    Returns the kwargs `record_comment_outcome` takes. A post or comment we cannot find is a
    graceful SKIP with a reason (deleted, private, removed) — never a fabricated zero, which would
    drag the reply rate down with data we never observed.
    """
    outcome = {"status": "checked", "skip_reason": None, "author_replied": False,
               "reply_count": 0, "like_count": 0, "visible_most_relevant": None,
               "our_reply_sent": False}
    driver.get(post_url)
    time.sleep(random.uniform(2.5, 4))
    _load_comment_thread(driver)

    items = _comment_items(driver)
    # The rendered comment thread is the page-native cross-check on the sort control (#1063): a post
    # that is deleted, private or has had its comments removed renders no thread AND no sort control,
    # which is the `post-unavailable` skip recorded below — working behaviour. Warning there filed a
    # grouped defect for a page that was simply gone; a thread that DID render and still yields no
    # control is selector rot and still warns. Same grading `--comment-outcome-url` applies.
    sort_label = _comment_sort_label(driver, wait, warn_on_miss=bool(items))
    ours = _find_our_comment(items, our_slug, comment_text)
    visible = None
    if ours is not None:
        # Only the DEFAULT sort answers the question this feature exists to ask.
        visible = True if sort_label == _SORT_MOST_RELEVANT else None
    elif sort_label == _SORT_MOST_RELEVANT and _switch_comment_sort(driver, wait, _SORT_MOST_RECENT):
        _load_comment_thread(driver)
        items = _comment_items(driver)
        ours = _find_our_comment(items, our_slug, comment_text)
        # Present under 'Most recent' but absent from 'Most relevant' IS the demotion signal.
        visible = False if ours is not None else None

    outcome["visible_most_relevant"] = visible
    if ours is None:
        outcome["status"] = "skipped"
        outcome["skip_reason"] = "post-unavailable" if not items else "comment-not-found"
        log_info(f"Comment outcome skipped ({outcome['skip_reason']}) on {post_url}",
                 user_id=user_id, action_type="scrape", task_name="sweep_comment_outcomes")
        return outcome

    # A rendered thread with an unreadable sort control is the #818 starvation signal: capture
    # candidate elements at DEBUG so the next locator iteration has fresh evidence.
    if not sort_label:
        candidates = _diagnose_sort_control_miss(driver)
        if candidates:
            log_debug("Comment sort control unreadable on rendered thread",
                      user_id=user_id, action_type="scrape", task_name="sweep_comment_outcomes",
                      post_url=post_url, candidates=candidates)

    replies = _thread_replies(driver, ours, items)
    author_href = _post_author_href(driver)
    author_slug = profile_slug(author_href)
    outcome["like_count"] = _comment_like_count(driver, ours)
    outcome["reply_count"] = sum(1 for _c, a in replies if not _href_is_profile(a, our_slug))
    outcome["our_reply_sent"] = any(_href_is_profile(a, our_slug) for _c, a in replies)
    outcome["author_replied"] = bool(author_slug) and any(
        _href_is_profile(a, author_slug) for _c, a in replies if not _href_is_profile(a, our_slug))
    return outcome


@shared_task.task(name='cqc_lem.app.run_automation.sweep_comment_outcomes',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_engage')
def sweep_comment_outcomes(self, user_id: int):
    """Revisit comments we posted ~24h ago and record what each one earned — author replies, likes,
    thread replies, and whether it is still visible under LinkedIn's default 'Most relevant' sort
    (issue #628). Read-only on LinkedIn: it navigates and reads, it never comments or reacts.
    """
    return _run_comment_outcomes_sweep(user_id)


def _run_comment_outcomes_sweep(user_id: int) -> str:
    """Body of sweep_comment_outcomes, extracted so it is unit-testable without the QueueOnce/Redis
    task wrapper.
    """
    targets = get_comment_outcome_targets(user_id, min_age_hours=_OUTCOME_MIN_AGE_HOURS,
                                          max_age_hours=_OUTCOME_MAX_AGE_HOURS,
                                          limit=_MAX_OUTCOME_CHECKS_PER_RUN)
    if not targets:
        return "No comments due an outcome check"
    lock_name = f"sweep_comment_outcomes:{user_id}"
    lock_token = acquire_run_lock(lock_name, ttl_seconds=1800)
    if lock_token is None:
        return "Skipped — another outcome sweep in progress"
    try:
        driver, wait, _email, my_profile = get_current_profile(user_id=user_id,
                                                              session_name="Comment Outcomes")
    except LinkedInRateLimited as e:
        log_warning("Comment outcome sweep skipped — rate-limited", exc=e, user_id=user_id,
                    task_name="sweep_comment_outcomes")
        release_run_lock(lock_name, lock_token)
        return "Skipped — rate limited"
    except Exception as e:
        log_error("Error starting comment outcome sweep", exc=e, user_id=user_id,
                  task_name="sweep_comment_outcomes")
        release_run_lock(lock_name, lock_token)
        return f"Failed to start comment outcome sweep: {e}"
    try:
        our_slug = profile_slug(str(my_profile.profile_url))
        if not our_slug:
            log_warning("Comment outcomes: no profile slug — cannot identify our own comments",
                        user_id=user_id, task_name="sweep_comment_outcomes")
            return "Skipped — no profile slug"
        checked = skipped = 0
        for row in targets:
            key = row.get("post_url")
            url = _post_url_from_key(key)
            if not url:
                continue
            try:
                outcome = _read_comment_outcome(driver, wait, user_id, url, our_slug,
                                                row.get("message") or "")
            except Exception as e:
                log_warning("Comment outcome check failed", exc=e, user_id=user_id,
                            task_name="sweep_comment_outcomes")
                continue
            record_comment_outcome(user_id, row.get("log_id"), post_key=key, **outcome)
            track_comment_outcome(user_id, row.get("log_id"), outcome, post_key=key)
            if outcome["status"] == "skipped":
                skipped += 1
            else:
                checked += 1
            time.sleep(random.uniform(4, 9))  # human pacing between post visits
        return f"Comment outcomes: checked {checked}, skipped {skipped} of {len(targets)} comment(s)"
    finally:
        quit_gracefully(driver)
        release_run_lock(lock_name, lock_token)


@shared_task.task(name='cqc_lem.app.run_automation.automate_reply_commenting',
                  bind=True, base=QueueOnce,
                  once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id', 'post_id']},
                  queue='se_engage')
def automate_reply_commenting(self, user_id: int, post_id: int, loop_for_duration: int = 60, future_forward=0):
    """Reply to recent comments left on a single post. Retained for the manual/API trigger and
    back-compat; the default post-publish path now uses sweep_reply_comments (event/scheduled mode).
    429-safe: a rate-limited session returns cleanly instead of dying before the re-queue.
    """
    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Reply to Comments")
    except LinkedInRateLimited as e:
        log_warning("Reply commenting skipped — LinkedIn rate-limited", exc=e, user_id=user_id,
                    task_name="automate_reply_commenting")
        return "Skipped — rate limited"
    except Exception as e:
        log_error("Error while getting profile for reply commenting", exc=e, user_id=user_id, task_name="automate_reply_commenting")
        return f"Failed to start reply commenting: {e}"

    try:

        start_time = datetime.now()

        # Stable VOICE synthesis reused across every reply in this run (voice source, not the raw JSON).
        profile_synthesis = get_or_create_profile_synthesis(user_id, my_profile)

        result = _reply_to_comments_on_open_post(driver, wait, user_id, post_id, my_profile,
                                                 profile_synthesis)["summary"]

        # Re-schedule the task in the queue for the future
        if loop_for_duration:
            elapsed_time = datetime.now() - start_time

            # If the loop for duration is more than 30 minutes, increase future forward timeby 5 minutes
            if loop_for_duration > (60 * 30):
                future_forward += 1

            # Future forward is an index. Use it to get the number of seconds to go forward
            future_forward_times = [0, 60 * 5, 60 * 10, 60 * 15, 60 * 30, 60 * 60]

            # Cap the future_forward to the length of the future_forward_times list
            if future_forward > len(future_forward_times) - 1:
                future_forward = len(future_forward_times) - 1

            future_forward_time = future_forward_times[future_forward]

            new_loop_for_duration = round(loop_for_duration - elapsed_time.total_seconds() - future_forward_time)
            frame = inspect.currentframe()
            current_function_name = frame.f_code.co_name
            args, _, _, values = inspect.getargvalues(frame)
            kwargs = {arg: values[arg] for arg in args}
            # myprint(f"{current_function_name} parameters: {kwargs}")

            if new_loop_for_duration < 0:
                log_info(f"Loop duration reached. Stopping {current_function_name} task...")
            else:
                # Change the value of the loop_for_duration and future_forward parameters
                kwargs['loop_for_duration'] = new_loop_for_duration
                kwargs['future_forward'] = future_forward
                # Add our function call back to the task queue
                log_info(
                    f"Adding {current_function_name} back to queue for {future_forward_time} seconds in the future...")
                # Remove 'self' from kwargs if it exists
                if 'self' in kwargs:
                    del kwargs['self']

                # Call self again in the future
                globals()[current_function_name].apply_async(kwargs=kwargs, countdown=future_forward_time)
    except Exception as e:
        log_error("Error while replying to comments", exc=e, user_id=user_id, post_id=post_id, task_name="automate_reply_commenting")
        result = f"Error while replying to comments: {e}"
    finally:
        quit_gracefully(driver)

    return result


@shared_task.task(name='cqc_lem.app.run_automation.update_stale_profile',
                  bind=True, base=QueueOnce, once={'graceful': True}, reject_on_worker_lost=True,
                  rate_limit='1/m', queue='se_outreach')
def update_stale_profile(self, user_id: int, force_refresh: bool = False):
    """Re-scrape the user's OWN LinkedIn profile and refresh the voice synthesis distilled from it.

    The scrape is a side effect of `get_current_profile`; the session is closed immediately because
    nothing else here needs a browser, and a Selenium slot held past its use is one an engagement
    lane wanted. A login failure returns a message string rather than raising, so one user's broken
    session shows up in that task's result instead of as a worker exception.

    `force_refresh` is what makes this task an ON-DEMAND refresh (issue #1076) rather than a daily
    sweep: without it a profile cached within the last day is simply read back, which is right for
    the beat and wrong for a user who edited their profile a minute ago and pressed the button. It
    also splits the `QueueOnce` key, so a manual refresh is deduped against other manual refreshes
    and never swallowed by an in-flight sweep.

    The synthesis refresh is best-effort and never fails a scrape that already succeeded.
    """
    log_info(f"Updating Stale Profile. User ID: {user_id}")
    try:
        driver, wait, user_email, my_profile = get_current_profile(
            user_id=user_id, session_name="Update Stale Profile", force_refresh=force_refresh)
    except Exception as e:
        log_error("Error while updating stale profile", exc=e, user_id=user_id, task_name="update_stale_profile")
        return f"Failed to update profile: {e}"
    quit_gracefully(driver)
    # A fresh scrape should yield a fresh voice synthesis — regenerate it now so the cached brief never
    # lags the profile it was distilled from. Best-effort: never fail the refresh over this.
    if my_profile is not None:
        try:
            synthesis = synthesize_profile(my_profile)
            if synthesis:
                set_profile_synthesis(user_id, synthesis)
        except Exception as e:
            log_warning("Could not refresh profile synthesis after scrape", exc=e, user_id=user_id,
                        task_name="update_stale_profile")
    return "Profile Updated Successfully"


def _affiliate_disclosure_gate(user_id: int, post_id: int, content: str,
                               first_comment_links: Optional[list] = None) -> Optional[str]:
    """Refuse to publish affiliate promotion that carries no FTC disclosure (issue #737).

    Returns None when the post may publish, or the task's return string when it may not. A blocked
    post is flagged ERROR rather than dropped: it is the author's own material and a human has to
    decide whether to add the disclosure or remove the link — silently publishing an undisclosed
    paid endorsement is the one outcome that is not available.

    Non-affiliate posts (virtually all of them) never touch a disclosure requirement, and any
    unexpected failure here publishes: this is a compliance check on a rare shape of post, not a new
    way for the whole posting path to break.
    """
    try:
        from cqc_lem.utilities.marketing.affiliate import disclosure_report
        graded = "\n".join([content or "", *(first_comment_links or [])])
        report = disclosure_report(graded, user_id=user_id)
    except Exception as e:
        log_warning("Affiliate disclosure check failed — publishing", exc=e,
                    user_id=user_id, post_id=post_id)
        return None
    if report.get("ok"):
        return None

    reason = report.get("reason")
    message = ("Post carries your referral link but no FTC affiliate disclosure — add the "
               "disclosure or remove the link, then re-approve."
               if reason == "missing_ftc_disclosure" else
               "Post carries a referral link but this deployment has no affiliate disclosure "
               "configured (AFFILIATE_DISCLOSURE_TEXT).")
    update_db_post_status(post_id, PostStatus.ERROR)
    log_error(f"Post blocked: {reason}", user_id=user_id, post_id=post_id, action_type="post")
    insert_new_log(user_id=user_id, action_type=LogActionType.POST, result=LogResultType.FAILURE,
                   post_id=post_id, message=message)
    try:
        from cqc_lem.utilities.observability import AFFILIATE_DISCLOSURE_BLOCKED, track_affiliate_event
        track_affiliate_event(AFFILIATE_DISCLOSURE_BLOCKED, user_id=user_id, post_id=post_id,
                              reason=reason)
    except Exception as e:
        log_warning("Could not track affiliate disclosure block", exc=e, user_id=user_id)
    return f"Post {post_id} flagged 'error' — {reason}"


@shared_task.task(name='cqc_lem.app.run_automation.post_to_linkedin',
                  bind=True, base=QueueOnce, once={'graceful': True, 'keys': ['post_id']}, reject_on_worker_lost=True,
                  rate_limit='2/m')
def post_to_linkedin(self, user_id: int, post_id: int):
    """Posts to LinkedIn using the LinkedIn API - https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/share-on-linkedin#creating-a-share-on-linkedin"""
    task_id = f"{self.request.id}-{user_id}-{post_id}"
    log_info(f"Post To LinkedIn | Task ID: {task_id}")

    # Skip if already posted — prevents duplicate posts when the task is re-queued
    if get_post_status(post_id) == PostStatus.POSTED.value:
        log_info(f"Post {post_id} already posted. Skipping duplicate execution.")
        return f"Post {post_id} already posted — skipped"

    # An occasion/milestone draft publishes through LinkedIn's native composer, which has no API
    # entity (issue #1074) — sharing it here would post it as an ordinary text update, losing the
    # entity the post exists for. The scheduler never dispatches one; this is the cross-check at the
    # single choke point every publish passes through, so any other caller is refused too.
    if get_post_manual_publish(post_id):
        log_warning("Refused to auto-publish a post marked for native (manual) publishing",
                    user_id=user_id, post_id=post_id, task_name="post_to_linkedin")
        return f"Post {post_id} publishes natively — skipped"

    # Login and publish post to LinkedIn
    user_email, user_password = get_user_password_pair_by_id(user_id)
    log_info(f"Posting to LinkedIn as user: {user_email}")

    # Get the post content
    content = get_post_content(post_id)

    prefs = get_engagement_preferences(user_id)

    # Link-in-first-comment (issue #392 - C3): an external link in the BODY costs ~60-68% reach, so
    # hold the link back here - the single choke point every post (generated OR hand-written) passes
    # through - and stash it for the seed comment dispatched below. Only links that will actually be
    # carried are removed, so nothing is ever silently lost. The split is in-memory only; the DB is
    # not touched until the publish succeeds, so a failed share leaves the original body+link intact
    # for a retry.
    content, first_comment_links = split_link_for_first_comment(
        content, enabled=bool(prefs.get("link_in_first_comment", True)))

    # FTC 16 CFR §255 (issue #737): extra trial time is compensation, so a post that publishes the
    # user's own referral link is a paid endorsement and must disclose the material connection IN
    # the content. Graded on the body AND the link the #392 split just carried out of it — a link in
    # the first comment is still the same post's endorsement, and grading the trimmed body alone
    # would let every affiliate post pass by having its link moved.
    gate = _affiliate_disclosure_gate(user_id, post_id, content, first_comment_links)
    if gate:
        return gate

    log_info(f"Posting to LinkedIn: {content}")

    post_type = get_post_type(post_id)
    log_info(f"Post type: {post_type}")

    if post_type in (PostType.CAROUSEL, PostType.DOCUMENT):
        slides = get_carousel_slides(post_id)
        label = "Document" if post_type == PostType.DOCUMENT else "Carousel"
        log_info(f"{label} slides ({len(slides)}): {slides}")
        # No slides, or no real per-slide images → don't post a placeholder deck.
        # Flag the post 'error' so it surfaces for manual/dev fix instead of failing silently.
        if not slides:
            urn = None
        elif post_type == PostType.DOCUMENT:
            # Native document/PDF: the same slides bundled into one swipeable deck.
            urn = share_document_on_linkedin(user_id, content, slides, post_id=post_id)
        else:
            urn = share_carousel_on_linkedin(user_id, content, slides)
        if not urn:
            # Only a missing/empty slide list is definitively an asset problem; a publish that
            # returned no URN could equally be missing credentials or an API failure, so don't
            # tell ops to go fix images when the deck may have been fine.
            reason = ("no real slide images" if not slides
                      else "publish returned no URN (check slide images, LinkedIn credentials and API logs)")
            update_db_post_status(post_id, PostStatus.ERROR)
            log_error(f"{label} not posted: {reason} — flagged 'error' for manual fix",
                      user_id=user_id, post_id=post_id, action_type="post", api_provider="linkedin")
            insert_new_log(user_id=user_id, action_type=LogActionType.POST, result=LogResultType.FAILURE,
                           post_id=post_id,
                           message=f"{label} not posted: {reason}. Status set to 'error'.")
            return f"Post {post_id} flagged 'error' — {label.lower()} not posted: {reason}"
    elif post_type == PostType.VIDEO:
        video_url = get_post_video_url(post_id)
        if video_url:
            log_info(f"Adding to Post | Video URL: {video_url}")
        urn = share_on_linkedin(user_id, content, video_url)
    else:
        # Single-image text post: attach the generated image when one exists. A missing image —
        # or even a failed lookup — never blocks the post: text publishes bare (the image is
        # enhancement, not a required asset, unlike a video post's video).
        try:
            from cqc_lem.utilities.db import get_post_image_url
            image_url = get_post_image_url(post_id)
        except Exception as e:
            log_warning("Could not read the post's image — publishing bare", exc=e,
                        user_id=user_id, post_id=post_id, action_type="post")
            image_url = None
        if image_url:
            log_info(f"Adding to Post | Image URL: {image_url}")
            urn = share_on_linkedin(user_id, content, image_url)
        else:
            urn = share_on_linkedin(user_id, content)

    if urn:
        post_url = f"https://www.linkedin.com/feed/update/{urn}/"
        log_info(f"Successfully created post using /posts API endpoint: {post_url}")

        # Update DB with status=posted
        update_db_post_status(post_id, PostStatus.POSTED)

        # Affiliate promotion that actually reached an audience (issue #770). Emitted from the
        # publish path rather than the writer, because "generated" and "published" are genuinely
        # different numbers — a promo post the author never approved is one of them and not the
        # other, and that gap is the honest read on whether the program is working.
        try:
            from cqc_lem.utilities.marketing.affiliate_content import record_promo_published
            record_promo_published(user_id, post_id, content,
                                   first_comment_links=first_comment_links, post_url=post_url)
        except Exception as e:
            log_warning("Could not track affiliate promo publish", exc=e, user_id=user_id,
                        post_id=post_id)

        # Only now — with the post actually live — persist the link split. Keeps the stored post in
        # sync with what published (the preview, the seed comment's grounding, and the post history
        # all read this back) without stranding the post in a link-held-back state if sharing failed.
        if first_comment_links:
            update_db_post_content(post_id, content)
            update_db_post_first_comment_link(post_id, "\n".join(first_comment_links))
            log_info(f"Held {len(first_comment_links)} link(s) back for the first comment",
                     user_id=user_id, post_id=post_id, action_type="post", task_name="post_to_linkedin")

        # Purge local media now that LinkedIn has re-hosted it — keeps the assets
        # volume bounded. Best-effort: never let cleanup failure break posting.
        try:
            from cqc_lem.utilities.utils import purge_post_assets
            purge_post_assets(post_id, video_url=get_post_video_url(post_id) if post_type == PostType.VIDEO else None)
        except Exception as e:
            log_info(f"purge_post_assets failed for post_id={post_id}: {e}")

        # Store the ACTUAL post body as the log message — not a status string. Seed comments and
        # thread replies read this back via get_post_message_from_log_for_user() to ground the AI in
        # the real post; a status string like "Successfully created post..." made the model write
        # comments about the /posts API instead of the post's subject.
        insert_new_log(user_id=user_id, action_type=LogActionType.POST, result=LogResultType.SUCCESS, post_id=post_id,
                       post_url=post_url,
                       message=content)

        # Seed the author's own FIRST comment ~3 min in (the post's golden hour). Dispatched HERE,
        # not from the scheduler: the seed needs the published post's URL, which only exists once
        # this task succeeds, and it is API-driven like posting itself (no Selenium, so the
        # active-user/throttle gates the scheduler applies to browser work don't belong on it).
        # This is also what guarantees a link held back above is actually delivered.
        auto_seed_comment_on_post.apply_async(kwargs={'user_id': user_id, 'post_id': post_id},
                                              countdown=3 * 60)

        # Reply/comment follow-up per the user's reply_check_mode (replaces the old 24h polling loop
        # that drove LinkedIn 429s). event → a golden-hour reply amplifier: several sweeps spread
        # across the first hour (#401) so every comment left while the post is being distributed gets
        # a timely reply, not just one at 35 min; scheduled → the beat dispatcher handles it; off →
        # nothing.
        reply_mode = prefs.get("reply_check_mode", "event")
        if reply_mode == "event":
            sweeps = _golden.reply_sweeps()
            # Distinct sweep_slot per sweep → distinct QueueOnce key, so celery-once enqueues all of
            # them; keyed only on user_id, the 2nd/3rd apply_async would be dropped as duplicates.
            for slot, countdown in enumerate(_golden_hour_sweep_countdowns(sweeps)):
                sweep_reply_comments.apply_async(kwargs={'user_id': user_id, 'sweep_slot': slot},
                                                 countdown=countdown)

        # Second wave (issue #622): ONE self-comment 6–8h out, when LinkedIn re-surfaces a post that
        # is still earning. Dispatched from here for the same reason the seed comment is — it needs
        # the published post's URL — and gated at run time (cap, pause, quality) rather than here,
        # so a re-dispatch can never stack a second one onto the same post.
        if _golden.second_wave_enabled():
            # First hop only — the task re-arms itself until its 6–8h offset is reached, so the
            # broker never holds a message longer than its visibility timeout (see the task).
            auto_second_wave_comment.apply_async(
                kwargs={'user_id': user_id, 'post_id': post_id},
                countdown=_golden.second_wave_first_countdown(user_id, post_id))

        return "Post successfully created"

    else:
        log_error("Failed to create post using /posts API endpoint", user_id=user_id, post_id=post_id, action_type="post", api_provider="linkedin")
        # Update DB with status=failed in the logs table
        insert_new_log(user_id=user_id, action_type=LogActionType.POST, result=LogResultType.FAILURE, post_id=post_id,
                       message="Failed to create post using /posts API endpoint.")

        return "Failed to create post using /posts API endpoint"
