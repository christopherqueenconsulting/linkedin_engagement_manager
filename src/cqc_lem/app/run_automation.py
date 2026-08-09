"""Every engagement action LEM takes AS the user, as Celery tasks on the Selenium lanes.

Feed commenting and replies, the self-comments (seed + second wave), DMs and their follow-up
ladders, the outreach and roster funnels, invites, group posts and the read-only outcome sweeps all
dispatch from here; the reusable Selenium mechanics they share live in `utilities/linkedin/*`. The
per-beat posture — what each one guarantees and why — is `docs/engagement-automation.md`.

Two rules cut across everything in this file.

**Pacing is not a safety gate.** `utilities/human_pacing.py` (issue #626) decides only how slowly we
act and fails OPEN; the hard stops are separate and checked in their own right — the 429 breaker in
`linkedin/rate_limit.py`, the suppression tripwire's engagement pause (#629), and the comment-quality
hold (#628, gated inside `automate_commenting` so every caller inherits it).

**Success is the OUTCOME being present, never a click having landed** (#1013, `docs/sdui-selenium-notes.md`).
A comment typed but not verifiably posted is a FAILURE row; a DM is sent only once it is visible in
the thread. The cost of getting this wrong is not a bad metric — it is the account.
"""

import hashlib
import inspect
import json
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv
from selenium.common import (
    ElementNotInteractableException,
    JavascriptException,
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver import ActionChains, Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

# Feed, groups and roster moved to `app.engagement.feed` (#1154) — the largest cluster, and the one
# that took the whole SDUI card engine with it. Three of these tasks are load-bearing HERE:
# `post_to_linkedin` dispatches `auto_seed_comment_on_post` and `auto_second_wave_comment`, and both
# `generate_and_post_comment` and `_fire_funnel_stage` dispatch `comment_on_post`. The rest are
# re-exported because `run_scheduler` and `api/routers/admin.py` import them from THIS module by
# name — as does `api/routers/user.py` for `get_feed_funnel`, which is why the one non-task in this
# list is here. Each task pins `name='cqc_lem.app.run_automation.<fn>'`, so the module path below is
# the only thing about them that changed.
#
# The ~100 private helpers that moved with them — the feed engine (`_score_feed_post`,
# `_engage_card`, `comment_on_feed_inline`), the roster tail (`comment_on_roster_posts`,
# `auto_follow_roster_target`, `advance_roster_connect`) and the group composer — are deliberately
# NOT re-exported. A re-export would keep `patch("...run_automation._score_feed_post")` resolving
# while the feed code reads its own module's binding — the patch would bind nothing and the test
# would pass having tested nothing. Leaving them absent makes that fail loudly at patch time.
from cqc_lem.app.engagement.feed import (  # noqa: E402  (after the local import block on purpose)
    auto_comment_in_groups,
    auto_draft_group_post,
    auto_post_to_group,
    auto_second_wave_comment,
    auto_seed_comment_on_post,
    auto_sync_user_groups,
    automate_commenting,
    comment_on_post,
    consolidate_duplicate_comments_for_user,
    get_feed_funnel,
)

# The connect rail moved to `app.engagement.invites` (#1154). Two of these are load-bearing here —
# `engage_with_profile_viewer` and `queue_roster_connect_invite` dispatch them by `.apply_async` —
# and the other three are re-exported because `run_scheduler` and `api.main` import them from THIS
# module by name. Each task pins `name='cqc_lem.app.run_automation.<fn>'`, so the module path below
# is now the only thing about them that changed.
#
# The five PRIVATE helpers that moved with them (`_profile_is_first_degree`,
# `_open_connect_invite_dialog`, `_add_connect_note`, `_submit_connect_invite`,
# `invite_to_connect_now`) are deliberately NOT re-exported. A re-export would keep
# `patch("...run_automation._profile_is_first_degree")` resolving while the invite code reads its
# own module's binding — the patch would bind nothing and the test would pass having tested nothing.
# Leaving them absent makes that fail loudly at patch time instead.
from cqc_lem.app.engagement.invites import (  # noqa: E402  (after the local import block on purpose)
    automate_invites_to_company_page_for_user,
    clean_stale_invites,
    invite_to_connect,
    send_connection_request,
    send_roster_connect_invite,
)

# The newsletter rail moved to `app.engagement.newsletter` (#1154). Nothing in THIS file calls these.
# `run_scheduler` imports `auto_publish_edition` and `track_newsletter_subscribers` from here by
# name, so those two are load-bearing; `auto_publish_newsletter_edition` is re-exported with them so
# the rail keeps ONE import path rather than two that differ for no reason a reader can see. Each
# task pins `name='cqc_lem.app.run_automation.<fn>'`, so the module path is all that changed.
#
# The seven PRIVATE helpers that moved with them (`_fill_edition_description`,
# `_fill_and_publish_article`, `_approved_cover_path`, `_tagged_edition_body`,
# `_parse_subscriber_count`, `_read_newsletter_subscriber_count`,
# `_invite_connections_to_newsletter`) are deliberately NOT re-exported, for the same reason as the
# connect rail's: a re-export would keep a stale `patch("...run_automation._approved_cover_path")`
# resolving while the newsletter code reads its own module's binding.
from cqc_lem.app.engagement.newsletter import (  # noqa: E402  (after the local import block on purpose)
    auto_publish_edition,
    auto_publish_newsletter_edition,
    track_newsletter_subscribers,
)
from cqc_lem.app.my_celery import app as shared_task
from cqc_lem.app.queue_once import QueueOnce
from cqc_lem.utilities import golden_hour as _golden
from cqc_lem.utilities.ai.ai_helper import (
    ai_check_message_history,
    generate_ai_response,
    generate_comment_reply_followup,
    generate_nurture_dm,
    generate_thread_reply,
    get_ai_message_refinement,
    get_or_create_profile_synthesis,
    lint_repaired,
    post_is_relevant,
    summarize_recent_activity,
    synthesize_profile,
)
from cqc_lem.utilities.ai.content_alignment import (
    ARTIFACT_KIND_LEAD_MAGNET,
    humanize_text,
    resolve_artifact_delivery,
    split_link_for_first_comment,
)
from cqc_lem.utilities.ai.dm_nurture import classify_reply_intent, is_stop_intent, nurture_delay_hours
from cqc_lem.utilities.audience_stats import (
    parse_connection_count,
    parse_follower_count,
    parse_profile_views,
    parse_search_appearances,
)
from cqc_lem.utilities.connection_targeting import (
    SOURCE_ADJACENT_POST,
    SOURCE_OWN_POST,
    CandidateSignal,
    rank_candidates,
    target_terms_from_prefs,
)
from cqc_lem.utilities.date import convert_viewed_on_to_date
from cqc_lem.utilities.db import (
    CATCHUP_CONTACT_CAP_WINDOW_DAYS,
    SCHEDULED_DM_SOURCE_ARTIFACT,
    SCHEDULED_DM_SOURCE_NURTURE,
    CatchupTouchStatus,
    ConnectionRequestStatus,
    LeadSignalChannel,
    LeadSignalSource,
    LeadSignalStatus,
    LogActionType,
    LogResultType,
    OutreachStage,
    OutreachStatus,
    PostStatus,
    PostType,
    ScheduledDmStatus,
    claim_appreciation_touch,
    claim_catchup_send_attempt,
    count_catchup_touches_for_contact_in_window,
    count_catchup_touches_sent_today,
    count_comments_today,
    count_dms_sent_today,
    count_followup_replies_today,
    count_invites_sent_today,
    count_open_connection_requests,
    count_open_outreach_targets,
    count_scheduled_dms_created_today,
    enqueue_followup,
    get_approved_outreach_targets,
    get_carousel_slides,
    get_catchup_touch,
    get_comment_followup,
    get_comment_outcome_targets,
    get_dm_history_for_profile,
    get_dm_template,
    get_due_followups,
    get_engagement_preferences,
    get_engagement_targets,
    get_engager_candidates,
    get_lead_magnet_settings,
    get_lead_signal,
    get_linkedin_profile_url_by_user_id,
    get_outreach_target_by_url,
    get_post_content,
    get_post_manual_publish,
    get_post_message_from_log_for_user,
    get_post_status,
    get_post_type,
    get_post_url_from_log_for_user,
    get_post_video_url,
    get_profile_facts,
    get_recent_comment_texts,
    get_recent_commented_rows_with_text,
    get_recent_navigable_commented_posts,
    get_recent_posted_post_ids,
    get_requested_person_keys,
    get_shipped_variant_keys,
    get_uncaptured_posted_post_ids,
    get_user_blog_url,
    get_user_id,
    get_user_password_pair_by_id,
    has_appreciation_touch,
    has_catchup_touch,
    has_engaged_url_with_x_days,
    has_open_scheduled_dm,
    has_received_lead_magnet,
    has_user_commented_on_post_url,
    insert_catchup_touch,
    insert_connection_request,
    insert_new_log,
    insert_outreach_target,
    insert_scheduled_dm,
    last_catchup_sent_at,
    mark_followup,
    max_catchup_touches_allowed,
    record_comment_followup,
    record_comment_outcome,
    record_follower_stat,
    record_lead_magnet_sent,
    record_post_stats,
    release_catchup_send_attempt,
    set_profile_synthesis,
    stop_followups_for_profile,
    update_catchup_touch_status,
    update_commented_post_key,
    update_db_post_content,
    update_db_post_first_comment_link,
    update_db_post_status,
    update_lead_signal,
    update_outreach_target,
    update_outreach_target_status,
    upsert_engager,
)
from cqc_lem.utilities.dm_templates import _draft_connect_note, render_dm_placeholders
from cqc_lem.utilities.golden_hour import _record_golden_hour_report, _reply_outcome
from cqc_lem.utilities.human_pacing import (
    ACTION_COMMENT,
    ACTION_DM,
    ACTION_REPLY,
    engagement_caps_from_prefs,
    pace_read,
    record_action,
    remaining_actions,
)
from cqc_lem.utilities.lead_scoring import (
    _author_display_name,
    _flag_lead_signal,
    _href_is_profile,
    person_key,
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
    _COMMENTLIST_TEXTBOX,
    _comment_container,
    _comment_header_author,
    _comment_items,
    _comment_items_from_thread,
    _reply_composer_for_comment,
    _reply_under_comment_inline,
    _type_and_submit_reply,
)
from cqc_lem.utilities.linkedin.helper import (
    clean_person_name,
    connection_degree,
    get_linkedin_profile_from_url,
    is_first_degree,
    load_profile_for_user,
    login_to_linkedin,
)
from cqc_lem.utilities.linkedin.message_thread import (
    ThreadState,
    name_from_profile_url,
    name_matches,
    open_addressed_composer,
    open_message_thread,
    read_last_message,
    read_last_sender,
    resolve_self_name,
)
from cqc_lem.utilities.linkedin.poster import (
    object_urn_from_post_url,
    share_carousel_on_linkedin,
    share_document_on_linkedin,
    share_on_linkedin,
)
from cqc_lem.utilities.linkedin.profile import LinkedInProfile
from cqc_lem.utilities.linkedin.rate_limit import (
    LinkedInRateLimited,
    _redis_client,
    acquire_run_lock,
    release_run_lock,
)
from cqc_lem.utilities.linkedin.session import get_current_profile
from cqc_lem.utilities.linkedin_formatter import normalize_public_text
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning
from cqc_lem.utilities.observability import (
    FEATURE_COMMENT,
    FEATURE_DM,
    attribute_llm_cost,
    llm_attribution,
    track_audience_snapshot,
    track_catchup_run,
    track_comment_outcome,
    track_post_outcome,
)
from cqc_lem.utilities.selenium_util import (
    click_element_wait_retry,
    click_first,
    find_all_first,
    find_first,
    get_driver_wait_pair,
    get_element_wait_retry,
    getText,
    quit_gracefully,
    wait_for_ajax,
)

__all__ = [
    # The feed / groups / roster tasks re-exported above, plus `get_feed_funnel`, which is not a
    # task: `api/routers/user.py` reads it from here to fill the reach panel.
    "auto_comment_in_groups",
    "auto_draft_group_post",
    "auto_post_to_group",
    "auto_second_wave_comment",
    "auto_seed_comment_on_post",
    "auto_sync_user_groups",
    "automate_commenting",
    "comment_on_post",
    "consolidate_duplicate_comments_for_user",
    "get_feed_funnel",
    # The connect-rail tasks re-exported above.
    "automate_invites_to_company_page_for_user",
    "clean_stale_invites",
    "invite_to_connect",
    "send_connection_request",
    "send_roster_connect_invite",
    # The newsletter-rail tasks re-exported above.
    "auto_publish_edition",
    "auto_publish_newsletter_edition",
    "track_newsletter_subscribers",
    # Names this module DEFINES for other modules to import. `run_scheduler` reads all four and
    # `zero_walk_verdict` is also read by tests, but nothing inside this file uses them — so CodeQL
    # reports them as unused globals unless the public surface is declared. Removing the 547 lines
    # of the connect rail re-fingerprinted those alerts as new, which is what surfaced them; the
    # names themselves are long-standing and live.
    "CATCHUP_PHASE_SEND",
    "CATCHUP_STATUS_DISPATCHED",
    "CATCHUP_STATUS_INACTIVE",
    "zero_walk_verdict",
]

# Load .env file
load_dotenv()


# Reading/thinking/typing delays all come from utilities/human_pacing.py (issue #626) — one engine,
# with a floor no human beats and a ceiling that keeps the sleep inline-safe.


def emoji_to_ue_string(emoji):
    """Converts an emoji to its equivalent escaped sequence."""
    return emoji.encode('unicode_escape').decode('ascii')


def clear_text_from_element(element: WebElement):
    """Empty a composer with select-all + Delete rather than `element.clear()`.

    LinkedIn's composers are contenteditable nodes, not form inputs, so the keystroke path is the one
    that reliably empties them — and it leaves the same input events behind that a person's typing
    would. `simulate_typing` uses this to recover a field after a JS emoji substitution fails.
    """
    # Select All
    element.send_keys(Keys.CONTROL + "a")
    # Delete what is selected
    element.send_keys(Keys.DELETE)


def simulate_typing(driver: WebDriver, editable_element: WebElement, text, allow_pauses: bool = True):
    """Type `text` into a composer one character at a time, with human-ish pauses between keys.

    Non-BMP characters (emoji) are the reason this is not a single `send_keys`: ChromeDriver throws
    on them, so each one is typed as a `|_n_|` placeholder and swapped back in afterwards with
    JavaScript. If that JS swap fails the field is rewritten WITHOUT the character rather than left
    holding a visible placeholder — losing an emoji is survivable, posting `|_1_|` is not.

    `allow_pauses=False` types at machine speed; use it only where nobody is watching the field.
    A key that will not send is warned about and skipped, so this can return having typed LESS than
    `text` — never assume the composer holds exactly what was passed in.
    """
    # Simulate typing the comment
    log_info("Typing Text...")
    type_speed_reducer = .5

    # Focus on Element
    actions = ActionChains(driver).move_to_element(editable_element).click()

    # Keep Track of characters to replace
    replacement_dict = {}

    for char in text:
        try:
            if ord(char) > 0xFFFF:
                # Generate a unique key for the character
                key = f"|_{len(replacement_dict) + 1}_|"
                # Record the character to replace later
                replacement_dict[key] = char

                # Insert the placeholder to replace later with JavaScript
                actions.send_keys(key)

                # Convert Emoji - THIS DOES NOT WORK
                # actions.send_keys(emoji_to_ue_string(char))

            else:
                actions.send_keys(char)
                # editable_element.send_keys(char)
        except Exception as e:
            log_warning("Error while sending char during typing", exc=e)

        if allow_pauses:
            type_pause = random.uniform(0.05 * type_speed_reducer, 0.15 * type_speed_reducer)
            # time.sleep(type_pause)  # Simulate human typing speed
            actions.pause(type_pause)

    actions.perform()

    script_pre = "arguments[0].value = arguments[0].value.replace(arguments[1],arguments[2]);"
    if editable_element.tag_name == 'p':
        script_pre = script_pre.replace(".value", ".innerText")

    for key, char in replacement_dict.items():
        try:
            # Use JavaScript to set the value for characters outside the BMP
            driver.execute_script(script_pre, editable_element, key, char)
        except JavascriptException as e:
            log_warning("Error while replacing char via JS", exc=e)
            # Get the current text
            current_text = getText(editable_element)
            # Remove the key from the text
            current_text = current_text.replace(key, '')
            # Clear all the text
            clear_text_from_element(editable_element)
            # Enter the new text without the char
            actions.send_keys(current_text).perform()

    if len(replacement_dict) > 0:
        # Send an additional space character (so changed register)
        actions.send_keys(Keys.SPACE).perform()

    log_info("Finished Typing!")


# The comment list mounts AFTER driver.get() returns — LinkedIn hydrates it client-side — so reading
# it on the first paint finds zero comments on a post that plainly has them. Polling is what makes
# this guard fire at all: without it the rebuild would be a second silently-never-firing check, which
# is the #966 defect itself. Bounded and cheap, and it stops as soon as the thread stops growing, so
# only a post with genuinely no comments pays the whole budget.
_COMMENT_THREAD_MOUNT_POLLS = 3
_COMMENT_THREAD_MOUNT_POLL_SECONDS = 1.0


def _thread_carries_our_comment(driver, my_profile: LinkedInProfile) -> bool:
    """True when a comment authored by US is already rendered in the open post's thread.

    Second line of defence behind the logs ledger — for a comment left before the ledger existed, or
    by hand. It reads the SDUI comment list `_comment_items` already maps and matches on the EXACT
    profile slug (`_href_is_profile`, never a substring), so a stranger's comment can't read as ours
    and silence a post we should engage. The `comments-comment-list__container` + `aria-label='• You'`
    XPath this replaces is a pre-SDUI anchor that has matched nothing since the rewrite, so the
    guard had silently stopped firing (issue #966).

    Deliberately does NOT call `_load_comment_thread`: that resizes the window to 1400x3400 to
    lazy-render an entire thread, which is a heavy price — and a viewport change the composer path
    then inherits — for a check the ledger already covers. Only the comments LinkedIn renders by
    default are read, and a miss falls through to commenting exactly as it did before.
    """
    slug = profile_slug(str(getattr(my_profile, "profile_url", "") or ""))
    if not slug:
        return False
    rendered = -1
    for attempt in range(_COMMENT_THREAD_MOUNT_POLLS):
        try:
            items = _comment_items(driver)
        except WebDriverException:
            return False
        if any(_href_is_profile(author, slug) for _tb, _cont, author in items):
            return True
        if items and len(items) == rendered:
            return False  # the thread rendered and stopped growing — none of it is ours
        rendered = len(items)
        if attempt < _COMMENT_THREAD_MOUNT_POLLS - 1:
            time.sleep(_COMMENT_THREAD_MOUNT_POLL_SECONDS)
    return False


def check_commented(driver, wait, user_id: int = None, post_url: str = None,
                    my_profile: LinkedInProfile = None) -> bool:
    """See if the current open url we've already posted on. The LinkedIn-side half only runs when
    the caller supplies `my_profile` — our own profile slug is what identifies our comment.
    """
    already_commented = False

    if post_url and post_url != driver.current_url:
        log_info(f"Navigating To: {post_url}")
        # Switch to post url
        driver.get(post_url)

    # 1. Check against Database (in logs table)
    if user_id and post_url:
        already_commented = has_user_commented_on_post_url(user_id, post_url)

    # 2. Check the rendered comment thread for a comment of ours
    if not already_commented and my_profile is not None:
        already_commented = _thread_carries_our_comment(driver, my_profile)

    return already_commented


# ── zero-walk tripwires (issues #1013, #1021) ────────────────────────────────────────────────
# The grading itself lives in utilities/linkedin/zero_walk.py, because scrapper and
# company_page_inviter need it too and both are imported BY this module. Re-exported under the
# names this module already used so every call site (and its tests) keeps one spelling.
# `app/engagement/feed.py` aliases the same upstream originals rather than importing these, which
# is what lets neither module import the other (#1154).
_CATCHUP_CARD_CROSSCHECK_SEL = "main div[role='listitem']"

zero_walk_verdict = _zw.zero_walk_verdict
_grade_zero_walk = _zw.grade_zero_walk
_report_zero_walk = _zw.report_zero_walk


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


# --- connect rung of the roster ladder (issue #979) ----------------------------------------------
# blocked -> follow (#962) -> STILL blocked -> needs connection -> (opt-in) auto-connect.


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


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
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


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
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


@shared_task.task(bind=True, base=QueueOnce,
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


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
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


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id', 'post_url']},
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


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
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


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
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


@shared_task.task(bind=True, base=QueueOnce,
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


# Ordered fallback chain for the invitation-manager cards. The pre-SDUI `invitation-card__container`
# class is gone, so prefer data-view-name, then the semantic list item, then any card that actually
# carries an Accept button.
_INVITATION_CARD_LOCATORS = [
    (By.CSS_SELECTOR, "div[data-view-name='invitation-card']"),
    (By.CSS_SELECTOR, "li[data-view-name='invitation-card']"),
    (By.XPATH, "//div[contains(@class,'invitation-card')]"),
    (By.XPATH, "//main//li[.//button[contains(@aria-label,'Accept')]]"),
]
_INVITATION_PROFILE_LINK_LOCATORS = [
    (By.CSS_SELECTOR, "a[data-view-name='invitation-card-profile-link']"),
    (By.CSS_SELECTOR, "a[href*='/in/']"),
]
_INVITATION_ACCEPT_LOCATORS = [
    (By.XPATH, ".//button[contains(@aria-label,'Accept')]"),
    (By.XPATH, ".//button[normalize-space()='Accept']"),
]


def accept_connection_request(user_id: int) -> dict[str, str]:
    """Accept pending connection requests and return {profile_url: name} for the ones we accepted.

    Zero pending invitations is the normal steady state, so an empty invitation manager returns an
    empty dict quietly — it is not an error. Each accept is paired with its own card so we only DM
    people whose click actually landed, and the cards are re-queried after every click because
    accepting re-renders the list (holding the original list went stale after the first accept).
    """
    user_email, user_password = get_user_password_pair_by_id(user_id)

    driver, wait = get_driver_wait_pair(session_name='Accept Connection Requests', user_id=user_id)

    invitation_data: dict[str, str] = {}

    try:
        login_to_linkedin(driver, wait, user_email, user_password)

        driver.get("https://www.linkedin.com/mynetwork/invitation-manager/")
        wait_for_ajax(driver)
        time.sleep(random.uniform(2, 4))

        pending = find_all_first(driver, _INVITATION_CARD_LOCATORS)
        if not pending:
            log_info("No pending connection invitations", user_id=user_id, action_type="accept_connection")
            return {}

        for _ in range(len(pending)):
            target = _next_pending_invitation(driver, set(invitation_data))
            if target is None:
                break
            profile_url, name, accept_button = target
            try:
                accept_button.click()
            except (StaleElementReferenceException, ElementNotInteractableException,
                    NoSuchElementException, WebDriverException) as e:
                log_warning("Could not accept a connection invitation", exc=e, user_id=user_id,
                            action_type="accept_connection")
                break
            if profile_url:
                invitation_data[profile_url] = name
            time.sleep(random.uniform(2, 4))

    except Exception as e:
        # Best-effort surface: a miss here costs us appreciation DMs for a cycle, nothing more.
        log_warning("Error while accepting connection requests", exc=e, user_id=user_id,
                    action_type="accept_connection")
    finally:
        quit_gracefully(driver)

    # Return the invitations list
    return invitation_data


def _next_pending_invitation(driver: WebDriver, accepted_urls: set[str]) -> tuple[str, str, WebElement] | None:
    """The next (profile_url, name, accept_button) still awaiting acceptance, read from a FRESH card
    query. Cards we already accepted are skipped by URL because LinkedIn sometimes leaves the accepted
    card in place instead of removing it.
    """
    for card in find_all_first(driver, _INVITATION_CARD_LOCATORS):
        link = _first_in_card(card, _INVITATION_PROFILE_LINK_LOCATORS)
        try:
            profile_url = _normalize_profile_url(link.get_attribute("href") or "") if link is not None else ""
            name = (getText(link) or "").strip().split("\n")[0] if link is not None else ""
        except (StaleElementReferenceException, NoSuchElementException):
            continue
        if profile_url and profile_url in accepted_urls:
            continue
        accept_button = _first_in_card(card, _INVITATION_ACCEPT_LOCATORS)
        if accept_button is None:
            continue
        return profile_url, name, accept_button
    return None


# --- Appreciation-DM sources beyond the invitation manager (issue #968) -------------------------
#
# Both surfaces below are STANDING lists, not event queues: a recommendation stays on the profile
# forever and a mention sits in the notifications feed for weeks. Two things therefore bound them
# and neither is optional — a lookback window (so a first run does not thank a five-year-old
# recommendation) and the durable claim in `appreciation_touches` (so the 60s re-queue of
# `automate_appreciation_dms_for_user` cannot thank the same person twice).
#
# OFF by default until the selectors below are live-grounded (`scripts/linkedin_live_validation.py
# --appreciation-sources`). An ungrounded scraper that finds nothing is a quiet no-op; one that
# finds the WRONG cards sends real DMs to real people, so the flip is the owner's.
_APPRECIATION_LOOKBACK_DEFAULT_DAYS = 30


def appreciation_sources_enabled() -> bool:
    """Read at the CALL SITE so the owner can flip it after grounding without a code change."""
    return os.getenv("APPRECIATION_SOURCES_ENABLED", "false").strip().lower() == "true"


# Recommendations Received, rebuilt on the live SDUI DOM (grounded 2026-08-03, issue #1007). The
# page carries NO `<li>`, NO `<time>`, NO `[data-view-name]` and nothing with `role='tab'` anywhere,
# so every rung of the original ladder was unmatchable and the scan read zero cards forever — the
# invisible no-op the grounding runs exist to catch. Its one `main div[role=list]` is the footer
# help-links list, never the recommendations list, so nothing anchors on that either.
#
# The tabs render as plain anchors/text. The bare URL already lands on Received, so the click below
# is only ever a correction and a miss is a no-op; `?detailScreenTabIndex=2` is Pending, whose rows
# are recommendation REQUESTS ("Requested"/"Sent") and never thank-worthy.
_RECOMMENDATION_TAB_LOCATORS = [
    (By.XPATH, "//main//a[normalize-space()='Received']"),
    (By.XPATH, "//main//button[normalize-space()='Received']"),
    (By.XPATH, "//main//*[@role='tab'][normalize-space()='Received']"),
]

# What IS on the page is one `/in/` anchor per card (its text is "name · degree · headline") sitting
# inside a block that also carries the card's "Month D, YYYY, X was Y's client" line. The read below
# climbs from each anchor to the OUTERMOST ancestor still about that ONE person — it stops the
# moment a second profile joins the block — and keeps the block only when it carries such a date.
# That drops the "Who your viewers also viewed" rail (undated rows) structurally rather than by
# class name, which is the whole point: hashed classes are all this page has left.
#
# One JS call returns every row at once, so nothing goes stale while the list re-renders under the
# walk — the same shape the profile-viewer rebuild (#1009) uses on the same DOM generation.
_RECOMMENDATION_ROWS_JS = r"""
const DATE = /(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s*\d{4}\b/;
const RAIL = /Who your viewers also viewed|People also viewed|Private to you/i;
const MAX_CLIMB = 15;
const root = document.querySelector('main') || document.body;
const slug = (a) => {
  const m = ((a.getAttribute('href') || '') || (a.href || '')).match(/\/in\/[^/?#]+/);
  return m ? decodeURIComponent(m[0]).toLowerCase() : '';
};
const anchors = [...root.querySelectorAll('a[href*="/in/"]')];
const rows = [];
const seen = new Set();
for (const anchor of anchors) {
  if (!slug(anchor)) continue;
  let block = anchor, hops = 0;
  while (block.parentElement && hops < MAX_CLIMB) {
    const parent = block.parentElement;
    if (parent === root || parent.tagName === 'BODY') break;
    const people = new Set([...parent.querySelectorAll('a[href*="/in/"]')].map(slug).filter(Boolean));
    if (people.size > 1) break;
    block = parent; hops++;
  }
  if (seen.has(block)) continue;
  seen.add(block);
  const text = block.innerText || '';
  if (!DATE.test(text) || RAIL.test(text)) continue;
  let name = '';
  for (const link of [anchor, ...block.querySelectorAll('a[href*="/in/"]')]) {
    const first = (link.innerText || '').split('\n').map(t => t.trim()).filter(Boolean)[0];
    if (first) { name = first; break; }
  }
  rows.push({href: anchor.href, name: name, text: text});
}
return {rows: rows,
        anchors: new Set(anchors.map(slug).filter(Boolean)).size,
        page_dated: DATE.test(root.innerText || '')};
"""

# The SDUI page paints asynchronously, so the first read of an empty list is not evidence the list
# is empty. Re-read a few times before believing it — an early zero reads exactly like an account
# with no recommendations, which is the confusion this whole issue is about.
_RECOMMENDATION_RENDER_ATTEMPTS = 5

# Mentions notifications — the closest thing LinkedIn exposes to "we did something together":
# somebody put this user's name in their own post or comment.
_MENTIONS_URL = "https://www.linkedin.com/notifications/?filter=mentions"
_MENTION_CARD_LOCATORS = [
    (By.CSS_SELECTOR, "main article[data-view-name='notification-card']"),
    (By.CSS_SELECTOR, "main div[data-view-name='notification-card']"),
    (By.XPATH, "//main//article[.//a[contains(@href,'/in/')]]"),
    (By.XPATH, "//main//li[.//a[contains(@href,'/in/')]][.//time or .//span]"),
]
_MENTION_ACTOR_LOCATORS = [
    (By.CSS_SELECTOR, "a[data-view-name='notification-actor']"),
    (By.CSS_SELECTOR, "a[href*='/in/']"),
]
# A mentions-filtered feed still mixes in "X posted", so the card has to SAY it was a mention.
_MENTION_TEXT_RE = re.compile(r"\b(mentioned|tagged)\s+you\b", re.IGNORECASE)
# The actor's name is normally the mention link's own text, but the live grounding run (#968) hit a
# card whose /in/ link carried NO text while the sentence right beside it read "Utkarsh Tiwari
# mentioned you in a comment in ...". Read the name back out of that sentence rather than greet a
# real person as "there". Bounded to the ≤5 punctuation-free words immediately before the verb, so
# the surrounding notification chrome ("Unread notification.") can never be read as a name.
_MENTION_ACTOR_NAME_RE = re.compile(
    r"((?:[^\s\n\r.,;:!?]+[ \t]+){0,4}[^\s\n\r.,;:!?]+)[ \t]+(?:mentioned|tagged)\s+you\b",
    re.IGNORECASE)
_RECOMMENDATION_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),\s*(\d{4})\b")
# LinkedIn writes notification ages compactly ("2h", "3d", "1w", "2mo", "1y") but spells them out on
# some surfaces, so both forms parse. Ordered so "months" can never be read as "minutes".
# The age has to be a STANDALONE token — a notification timestamp always is, and a card carries the
# quoted post/comment too, where "$5m ARR" or "v2h" would otherwise read as "posted moments ago" and
# make a two-year-old mention look like today's. A `\b` before the digits is not enough for that:
# `$5m` clears one. Prose that still parses ("10 years of experience") can only push the age OUT of
# the window, which skips — the safe direction for a surface that DMs real people.
_RELATIVE_AGE_RE = re.compile(
    r"(?:^|(?<=[\s•·|(\[]))(\d{1,3})\s*"
    r"(mo(?:nths?)?|min(?:utes?)?|h(?:ours?)?|d(?:ays?)?|w(?:eeks?)?|y(?:ears?)?|m)\b",
    re.IGNORECASE)
# Anything under a day is "today" for a lookback measured in days.
_RELATIVE_AGE_UNIT_DAYS = (("mo", 30.0), ("min", 0.0), ("h", 0.0), ("d", 1.0), ("w", 7.0),
                           ("y", 365.0), ("m", 0.0))


def appreciation_lookback_days() -> int:
    """How far back a standing surface may be read. Anything older is somebody else's history, not
    a moment worth reacting to.
    """
    try:
        return max(1, int(os.getenv("APPRECIATION_LOOKBACK_DAYS")
                          or _APPRECIATION_LOOKBACK_DEFAULT_DAYS))
    except ValueError:
        return _APPRECIATION_LOOKBACK_DEFAULT_DAYS


def _parse_recommendation_date(text: str, now: datetime = None) -> "float | None":
    """Age in days of the newest 'Month D, YYYY' in a recommendation card, or None when the card
    carries no readable date. None means SKIP — an undated card could be from 2018.
    """
    matches = _RECOMMENDATION_DATE_RE.findall(text or "")
    if not matches:
        return None
    moment = now or datetime.now(timezone.utc)
    ages = []
    for month, day, year in matches:
        try:
            received = datetime.strptime(f"{month} {day} {year}", "%B %d %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        ages.append((moment - received).total_seconds() / 86400.0)
    return min(ages) if ages else None


def _parse_relative_age_days(text: str) -> "float | None":
    """Age in days from a LinkedIn relative timestamp ('2h', '3d', '2mo'). None when nothing in the
    card reads as an age — which, like an undated recommendation, means SKIP.
    """
    match = _RELATIVE_AGE_RE.search(text or "")
    if not match:
        return None
    amount, unit = match.group(1), match.group(2).lower()
    for prefix, days in _RELATIVE_AGE_UNIT_DAYS:
        if unit.startswith(prefix):
            return float(amount) * days
    return None


def _card_person(card: WebElement, locators: list) -> tuple:
    """(normalized profile_url, display name) for the person a card is about, or ('', '')."""
    link = _first_in_card(card, locators)
    if link is None:
        return "", ""
    try:
        href = link.get_attribute("href") or ""
        raw_name = (getText(link) or "").strip().split("\n")[0]
    except (StaleElementReferenceException, NoSuchElementException, WebDriverException):
        return "", ""
    if "/in/" not in href:
        return "", ""
    return _normalize_profile_url(href), clean_person_name(raw_name)


def _mention_actor_name(text: str) -> str:
    """The mentioner's name recovered from the card's own sentence, for when the actor link rendered
    without text. '' when nothing in it names anybody — the DM then opens with "Hi there", which is
    a DELIBERATE fallback: a thank-you addressed generically still beats not thanking someone who
    publicly featured this user.
    """
    match = _MENTION_ACTOR_NAME_RE.search(text or "")
    return clean_person_name(match.group(1)) if match else ""


def _card_text(card: WebElement) -> str:
    try:
        return getText(card) or ""
    except (StaleElementReferenceException, NoSuchElementException, WebDriverException):
        return ""


def _recommendation_reading(driver, user_id: int = None) -> dict:
    """One JS read of the recommendations page: the resolved cards plus the two numbers that tell
    an empty section apart from selectors that have rotated again.

    `page_dated` is the tripwire — the page plainly showing "Month D, YYYY" while no block resolves
    is drift, whereas neither is just an account nobody has recommended.
    """
    empty = {"rows": [], "anchors": 0, "page_dated": False}
    try:
        reading = driver.execute_script(_RECOMMENDATION_ROWS_JS)
    except WebDriverException as e:
        log_warning("Could not read the recommendations page", exc=e, user_id=user_id,
                    action_type="scrape")
        return empty
    if not isinstance(reading, dict):
        return empty
    return {"rows": [row for row in (reading.get("rows") or []) if isinstance(row, dict)],
            "anchors": int(reading.get("anchors") or 0),
            "page_dated": bool(reading.get("page_dated"))}


def get_recent_recommendations(driver, wait, user_id: int = None,
                               profile_url: str = None) -> dict[str, str]:
    """Return {profile_url: name} for people who recommended this user inside the lookback window.

    Reads the user's OWN profile → Recommendations → Received. Undated cards are skipped rather
    than thanked: the section carries every recommendation ever received, so "no date" and "this
    week" must never collapse into the same answer.
    """
    if not appreciation_sources_enabled():
        return {}

    own_url = _normalize_profile_url(profile_url or "") or _own_profile_url(driver, user_id)
    if not own_url:
        log_debug("No own profile URL — skipping the recommendations scan", user_id=user_id,
                  action_type="scrape")
        return {}

    driver.get(f"{own_url}/details/recommendations/")
    wait_for_ajax(driver)
    time.sleep(random.uniform(2, 4))
    # The Received tab is the default; clicking it is only a correction, so a miss is a no-op.
    click_first(driver, wait, _RECOMMENDATION_TAB_LOCATORS, "Recommendations Received tab",
                required=False, max_try=1, warn_on_miss=False, user_id=user_id)
    time.sleep(random.uniform(1, 2))

    reading = {"rows": [], "anchors": 0, "page_dated": False}
    for attempt in range(_RECOMMENDATION_RENDER_ATTEMPTS):
        reading = _recommendation_reading(driver, user_id)
        if reading["rows"] or reading["page_dated"]:
            break
        if attempt + 1 < _RECOMMENDATION_RENDER_ATTEMPTS:
            time.sleep(2)

    if not reading["rows"]:
        if reading["page_dated"]:
            # The page renders dated recommendations and not one block resolved around them — that
            # is selector drift, which is exactly how this source went silently dead once already.
            log_warning("Recommendations page shows dated cards but none resolved", user_id=user_id,
                        action_type="scrape", url=f"{own_url}/details/recommendations/")
        else:
            log_debug("No recommendation cards rendered", user_id=user_id, action_type="scrape")
        return {}

    lookback = appreciation_lookback_days()
    recommenders: dict[str, str] = {}
    dated = 0
    for row in reading["rows"]:
        # Every row already carried a date-shaped line, but the parser is still the authority:
        # "February 30, 2026" matches the shape and is not a date.
        age_days = _parse_recommendation_date(row.get("text") or "")
        if age_days is None:
            continue
        dated += 1
        if age_days > lookback:
            continue
        url = _normalize_profile_url(row.get("href") or "")
        if not url or url == own_url:
            continue
        recommenders.setdefault(url, clean_person_name(row.get("name") or ""))

    if not dated:
        # `page_dated` only catches the page-vs-reader split; this is the reader-vs-parser one.
        # Blocks resolved, every one carried a date-SHAPED line, and not one parsed — that is
        # format drift, and without this it reads as "no recent recommendations" forever.
        log_warning("Recommendation cards carried no readable date", user_id=user_id,
                    action_type="scrape", url=f"{own_url}/details/recommendations/")
    log_info(f"Found {len(recommenders)} recommendation(s) received in the last {lookback} day(s)",
             user_id=user_id, action_type="dm")
    return recommenders


def get_recent_collaborators(driver, wait, user_id: int = None) -> dict[str, str]:
    """Return {profile_url: name} for people to thank for a recent collaboration.

    LinkedIn exposes no "collaboration" event, so the source is the nearest structured one it does
    expose: the mentions notification feed — somebody put this user's name in their own post or
    comment inside the lookback window. A card that does not SAY it was a mention is skipped, as is
    one with no readable age.
    """
    if not appreciation_sources_enabled():
        return {}

    driver.get(_MENTIONS_URL)
    wait_for_ajax(driver)
    time.sleep(random.uniform(2, 4))

    cards = find_all_first(driver, _MENTION_CARD_LOCATORS)
    if not cards:
        log_debug("No mention notifications rendered", user_id=user_id, action_type="scrape")
        return {}

    lookback = appreciation_lookback_days()
    collaborators: dict[str, str] = {}
    for card in cards:
        text = _card_text(card)
        if not _MENTION_TEXT_RE.search(text):
            continue
        age_days = _parse_relative_age_days(text)
        if age_days is None or age_days > lookback:
            continue
        url, name = _card_person(card, _MENTION_ACTOR_LOCATORS)
        if not url:
            continue
        collaborators.setdefault(url, name or _mention_actor_name(text))

    log_info(f"Found {len(collaborators)} collaboration mention(s) in the last {lookback} day(s)",
             user_id=user_id, action_type="dm")
    return collaborators


def _own_profile_url(driver, user_id: "int | None") -> str:
    """The user's own profile URL: the stored one first, else resolved live by letting LinkedIn's
    /in/me/ redirect answer it. Empty string when neither works.
    """
    if user_id is not None:
        try:
            stored = get_linkedin_profile_url_by_user_id(user_id)
        except Exception as e:
            log_debug(f"Could not read stored profile URL: {e}", user_id=user_id, action_type="scrape")
            stored = None
        if stored:
            return _normalize_profile_url(stored)
    try:
        driver.get("https://www.linkedin.com/in/me/")
        wait_for_ajax(driver)
        time.sleep(random.uniform(1, 2))
        resolved = _normalize_profile_url(driver.current_url or "")
    except WebDriverException as e:
        log_debug(f"Could not resolve own profile URL: {e}", user_id=user_id, action_type="scrape")
        return ""
    return resolved if "/in/" in resolved else ""


@attribute_llm_cost(FEATURE_DM)
def build_dm_from_template(user_id: int, event_type: str, first_name: str,
                           my_profile: LinkedInProfile, step: int = 0, blog_url: str = "",
                           event_detail: str = "") -> "str | None":
    """Render the user's DM template for an event (filling {first_name}/{headline}/{blog_url}/
    {event_detail}) and LLM-refine it to their voice (<=300 chars). Falls back to the code-default
    template; returns None only when no template exists for that (event, step).
    """
    tmpl = get_dm_template(user_id, event_type, step)
    if not tmpl:
        return None
    headline = getattr(my_profile, "job_title", None) or "my professional field"
    rendered = render_dm_placeholders(tmpl["template_text"], first_name=first_name,
                                      headline=headline, blog_url=blog_url,
                                      event_detail=event_detail)
    def _refine(fix_directive: str = "") -> str:
        refined = get_ai_message_refinement(rendered, character_limit=300,
                                            extra_directive=fix_directive)
        # Humanization pass (issue #416 — A5): de-slop the DM before it's sent. Fails open and keeps
        # the pre-humanize text if a rewrite would exceed the 300-char DM budget.
        return humanize_text((refined or rendered).strip(), content_type="dm", max_chars=300)

    try:
        # Deterministic slop lint + bounded re-refine (issue #625 / D1). A DM has no review queue,
        # so a still-slopped one is sent with the patterns named in the log rather than dropped —
        # dropping it would silently break the outreach sequence.
        return lint_repaired(_refine(), "dm", _refine, user_id=user_id, action_type="dm")
    except Exception as e:
        log_warning("DM refinement failed; sending rendered template", exc=e, action_type="dm", user_id=user_id)
        return rendered.strip()


# How long after an outbound DM we come back to look for a reply when the user has configured no
# further step in that sequence. Long enough that a same-day reply has landed, short enough that the
# thread is still warm when we draft the next message.
_REPLY_CHECK_DEFAULT_DELAY_HOURS = 48


def _reply_check_delay_hours() -> int:
    try:
        return max(1, int(os.environ.get("DM_REPLY_CHECK_DELAY_HOURS")
                          or _REPLY_CHECK_DEFAULT_DELAY_HOURS))
    except ValueError:
        return _REPLY_CHECK_DEFAULT_DELAY_HOURS


def enqueue_next_followup(user_id: int, profile_url: str, first_name: str, event_type: str, current_step: int) -> None:
    """If a follow-up template exists for the next step, schedule it at now + its delay_hours.
    due_at is stored as naive UTC to match the rest of the system (see get_due_followups).

    When there is NO next step, schedule a REPLY CHECK at that same step anyway (issue #623). The
    stock templates are step-0 only, so this branch used to end the thread the moment the first DM
    went out: nothing was ever queued in dm_followups, process_user_followups therefore never ran,
    nobody's reply was ever read, and the #485 auto-nurture that turns a reply into an approval-gated
    next message could not fire — scheduled_dms had zero rows in production from V53 to now. The
    check costs one thread open: a reply becomes a nurture draft, and silence hits the existing
    "no template for this step" branch in process_user_followups and stops the sequence. This is the
    same shape _schedule_catchup_followup already used for catch-up touches (#482), generalized to
    every sequence.
    """
    try:
        nxt = get_dm_template(user_id, event_type, current_step + 1)
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        if nxt:
            due = now_utc + timedelta(hours=int(nxt.get("delay_hours", 24) or 24))
            enqueue_followup(user_id, profile_url, first_name, event_type, current_step + 1, due)
            return
        if not _nurture_enabled() or str(event_type) == NURTURE_EVENT_TYPE:
            # Nurture schedules its own re-check; with nurture off there is nothing to check for.
            log_info(f"No step {current_step + 1} template for '{event_type}' — sequence ends here",
                     user_id=user_id, action_type="followup")
            return
        due = now_utc + timedelta(hours=_reply_check_delay_hours())
        enqueue_followup(user_id, profile_url, first_name, event_type, current_step + 1, due)
        log_info(f"No step {current_step + 1} template for '{event_type}' — queued a reply check "
                 f"in {_reply_check_delay_hours()}h so a reply can become a nurture draft",
                 user_id=user_id, action_type="followup")
    except Exception as e:
        log_warning("Failed to enqueue next follow-up", exc=e, action_type="followup", user_id=user_id)


def _last_inbound_message(driver) -> str:
    """Text of the newest message in the ALREADY-OPEN thread. Best-effort — returns '' when the DOM
    doesn't match (detection simply finds nothing rather than breaking the follow-up run).
    """
    return read_last_message(driver)


def check_dm_replied(driver, wait, profile_url: str, my_name: str = None,
                     person_name: str = None, user_id: int = None) -> ThreadState:
    """Has this person replied since our last message? Opens their thread through the #731
    resolution ladder (`open_message_thread`) and reads the sender of the most recent message group.

    Returns THREE states, never a bool. UNKNOWN — no route opened a thread, nothing readable in it,
    or we don't know our own name to compare against — means *we could not tell*, and the caller
    must skip the follow-up. Before #731 that case collapsed into False ('no reply → keep sending'),
    which is how a live selector rotation turned into follow-ups sent to people who had answered.
    """
    try:
        opened = open_message_thread(driver, wait, profile_url, person_name=person_name,
                                     user_id=user_id)
        if not opened.opened:
            return ThreadState.UNKNOWN
        last_sender = read_last_sender(driver)
        if not last_sender:
            log_warning(f"Reply-detection: thread opened via '{opened.route}' but no message sender "
                        f"could be read — treating as UNKNOWN, not as 'no reply'",
                        user_id=user_id, action_type="followup")
            return ThreadState.UNKNOWN
        if not (my_name or "").strip():
            log_warning("Reply-detection: no self-name to compare the last sender against — "
                        "treating as UNKNOWN, not as 'no reply'",
                        user_id=user_id, action_type="followup")
            return ThreadState.UNKNOWN
        if name_matches(my_name, last_sender):
            return ThreadState.NOT_REPLIED  # we spoke last → no reply yet
        return ThreadState.REPLIED  # someone other than us spoke last → they replied
    except Exception as e:
        log_warning("Reply-detection failed (treating as UNKNOWN)", exc=e, user_id=user_id,
                    action_type="followup")
        return ThreadState.UNKNOWN


# --- DM conversation auto-nurture (issue #485) --------------------------------------------------
# A reply used to END a DM sequence: we stopped the follow-ups and the thread went cold. Now the
# same reply we ALREADY read (for #483 intent detection) branches into a drafted next message that
# lands APPROVAL-GATED in the scheduled-DM queue the operator already reviews. Nothing here can send
# anything: the draft is 'pending' until a human approves it in the UI, and delivery then runs
# through send_scheduled_dm with the existing per-day DM cap.
NURTURE_EVENT_TYPE = "nurture"  # dm_templates / dm_followups event_type for this sequence
# Re-check touches per thread. Each one is a Selenium thread-open, so the walk is bounded: after
# this many rounds a conversation that is going nowhere stops costing us sessions.
_NURTURE_MAX_STEPS = 3
_NURTURE_DEFAULT_MAX_PER_DAY = 5


def _nurture_enabled() -> bool:
    raw = os.environ.get("DM_NURTURE_ENABLED")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _nurture_auto_approve() -> bool:
    """OFF by default — a drafted reply to a real prospect is exactly the thing a human should see
    before it sends. Turning this on skips the approval queue (the draft goes straight to 'approved'
    and the scanner delivers it at its slot).

    Only an explicit affirmative opens the gate: unset, blank/whitespace, and anything unrecognized
    all keep the human in the loop. This is the one flag where a typo must fail CLOSED.
    """
    raw = os.environ.get("DM_NURTURE_AUTO_APPROVE") or ""
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _nurture_max_per_day() -> int:
    try:
        return max(0, int(os.environ.get("DM_NURTURE_MAX_PER_DAY") or _NURTURE_DEFAULT_MAX_PER_DAY))
    except ValueError:
        return _NURTURE_DEFAULT_MAX_PER_DAY


def _nurture_after_reply(user_id: int, followup: dict, their_message: str,
                         my_profile: LinkedInProfile, prefs: dict = None,
                         profile_synthesis: str = None) -> "int | None":
    """Draft the context-aware next message for a thread whose lead just replied, and queue it for
    approval. Returns the scheduled_dms id, or None when nothing was drafted (disabled, explicit
    disinterest, already drafted for this thread, daily cap, or no usable draft).

    Best-effort and NON-FATAL — nurturing a conversation must never break the follow-up run.
    """
    profile_url = followup.get("profile_url") or ""
    first_name = followup.get("first_name") or ""
    try:
        if not _nurture_enabled():
            log_warning("DM nurture is disabled (DM_NURTURE_ENABLED) — a reply was read but no next "
                        "message will be drafted", user_id=user_id, action_type="dm")
            return None
        if not str(their_message or "").strip():
            log_warning(f"DM nurture: a reply from {first_name or profile_url} was detected but its "
                        f"text could not be read — nothing to draft against",
                        user_id=user_id, action_type="dm")
            return None

        verdict = classify_reply_intent(their_message)
        intent = verdict.get("intent")
        if is_stop_intent(intent):
            # An explicit no. The caller has already stopped the sequence; we add nothing and never
            # re-open this thread — no draft, no re-check.
            log_info(f"DM nurture: {first_name or profile_url} declined — stopping the thread",
                     user_id=user_id, action_type="dm")
            return None

        # One drafted next message per conversation, across BOTH auto-drafting mechanics. A thread
        # re-checked before the operator has acted must not stack a second draft on the same reply —
        # and neither must an owned-asset delivery already queued on this thread (#624), or the
        # person ends up with two pending messages, which is the spam shape both gates exist to stop.
        if (has_open_scheduled_dm(user_id, profile_url, source=SCHEDULED_DM_SOURCE_NURTURE)
                or has_open_scheduled_dm(user_id, profile_url, source=SCHEDULED_DM_SOURCE_ARTIFACT)):
            log_info(f"DM nurture: {first_name or profile_url} already has a queued draft; skipping",
                     user_id=user_id, action_type="dm")
            return None

        cap = _nurture_max_per_day()
        if count_scheduled_dms_created_today(user_id, source=SCHEDULED_DM_SOURCE_NURTURE) >= cap:
            log_info(f"DM nurture: daily draft cap ({cap}) reached", user_id=user_id, action_type="dm")
            return None

        # A 'nurture' follow-up means we are already IN this sequence — continue it at the next step.
        step = (int(followup.get("next_step") or 0) + 1
                if str(followup.get("event_type")) == NURTURE_EVENT_TYPE else 0)
        tmpl = get_dm_template(user_id, NURTURE_EVENT_TYPE, step)
        message = None
        try:
            message = generate_nurture_dm(
                their_message, intent, my_profile, first_name=first_name,
                template_hint=(tmpl or {}).get("template_text"),
                history=get_dm_history_for_profile(user_id, profile_url),
                prefs=prefs, profile_synthesis=profile_synthesis)
        except Exception as e:
            log_warning("Nurture draft failed; falling back to the template", exc=e,
                        user_id=user_id, action_type="dm")
        if not message:
            message = build_dm_from_template(user_id, NURTURE_EVENT_TYPE, first_name, my_profile, step=step)
        if not message:
            log_warning(f"DM nurture: no draft could be produced for {first_name or profile_url} "
                        f"(step {step}) — the LLM returned nothing and no 'nurture' template exists "
                        f"for that step", user_id=user_id, action_type="dm")
            return None

        due = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=nurture_delay_hours(intent))
        status = ScheduledDmStatus.APPROVED if _nurture_auto_approve() else ScheduledDmStatus.PENDING
        dm_id = insert_scheduled_dm(user_id, profile_url, message, due,
                                    recipient_name=first_name or None, status=status,
                                    source=SCHEDULED_DM_SOURCE_NURTURE)
        if not dm_id:
            log_warning(f"DM nurture: drafted a next message for {first_name or profile_url} but "
                        f"the scheduled_dms insert failed", user_id=user_id, action_type="dm")
            return None
        log_info(f"DM nurture: drafted a '{intent}' next message for {first_name or profile_url} "
                 f"(step {step}, {status})", user_id=user_id, action_type="dm")

        # Keep the thread on the follow-up sequencer so a further reply gets its own next message.
        if step + 1 < _NURTURE_MAX_STEPS:
            enqueue_followup(user_id, profile_url, first_name, NURTURE_EVENT_TYPE, step,
                             due + timedelta(hours=nurture_delay_hours(intent)))
        return dm_id
    except Exception as e:
        log_warning("DM nurture failed", exc=e, user_id=user_id, action_type="dm")
        return None


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_outreach')
def process_user_followups(self, user_id: int, max_per_run: int = 20):
    """Send this user's due DM follow-ups: anyone who has replied gets their sequence stopped and,
    instead of going cold, an approval-gated context-aware next message (issue #485); everyone else
    gets the next-step template rendered in the user's voice, sent, marked, and re-scheduled.
    """
    due = [f for f in get_due_followups(datetime.now(timezone.utc).replace(tzinfo=None))
           if f["user_id"] == user_id]
    if not due:
        # Not routine: this task is only dispatched for users who HAVE due rows, so an empty list
        # means the row was consumed between dispatch and run — or nothing is being enqueued at all,
        # which is exactly how the nurture queue stayed empty for months (issue #623).
        log_warning("Follow-up run found nothing due — no reply will be read and no nurture draft "
                    "will be queued for this user", user_id=user_id,
                    task_name="process_user_followups", action_type="followup")
        return "No due follow-ups"
    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Follow-ups")
    except Exception as e:
        log_error("Error getting profile for follow-ups", exc=e, user_id=user_id, task_name="process_user_followups")
        return f"Failed to start follow-ups: {e}"
    sent = 0
    nurtured = 0
    skipped = 0
    lead_ctx: dict = {}  # voice context for lead drafts — fetched lazily, only if someone replies
    # Resolved ONCE per run: the saved display name (Settings, required) with the scraped profile as
    # the fallback. Empty means every thread reads UNKNOWN and nothing is sent — which is the point.
    self_name = resolve_self_name(user_id, my_profile)
    if not self_name:
        log_warning("No LinkedIn display name saved and none on the cached profile — every "
                    "follow-up in this run will be skipped as unreadable. Set it under "
                    "Settings > Setup & Connection.", user_id=user_id, action_type="followup",
                    task_name="process_user_followups")
    try:
        for f in due[:max_per_run]:
            state = check_dm_replied(driver, wait, f["profile_url"], my_name=self_name,
                                     person_name=f.get("first_name"), user_id=user_id)
            if state is ThreadState.UNKNOWN:
                # We could not read the thread, so we do NOT know whether they answered. Sending
                # anyway is the one irreversible mistake here (issue #731) — leave the row due so
                # the next run re-reads it, and let the miss be greppable.
                skipped += 1
                log_warning(f"Follow-up skipped: could not read the thread with "
                            f"{f.get('first_name') or f['profile_url']} — deferring to the next run",
                            user_id=user_id, action_type="followup",
                            task_name="process_user_followups")
                continue
            if state is ThreadState.REPLIED:
                # Their reply is on screen already — read it once and use it twice: buying-intent
                # detection (#483) and the auto-nurture next message (#485).
                if not lead_ctx:
                    lead_ctx = {"prefs": get_engagement_preferences(user_id),
                                "synthesis": get_or_create_profile_synthesis(user_id, my_profile)}
                their_message = _last_inbound_message(driver)
                _flag_lead_signal(user_id, their_message, LeadSignalSource.DM,
                                  "thread", person_name=f.get("first_name"),
                                  person_profile_url=f["profile_url"], channel=LeadSignalChannel.DM,
                                  context_url=f["profile_url"], my_profile=my_profile,
                                  prefs=lead_ctx["prefs"], profile_synthesis=lead_ctx["synthesis"])
                # Stop the old sequence FIRST — the nurture path enqueues its own re-check, and a
                # blanket stop afterwards would cancel it.
                stop_followups_for_profile(user_id, f["profile_url"])
                mark_followup(f["id"], "stopped")
                if _nurture_after_reply(user_id, f, their_message, my_profile,
                                        prefs=lead_ctx["prefs"], profile_synthesis=lead_ctx["synthesis"]):
                    nurtured += 1
                elif not is_stop_intent(classify_reply_intent(their_message, use_llm=False)["intent"]):
                    # Nurture (#485) owns the drafted next message whenever it produced one, so the
                    # catch-up funnel hand-off (#482) is the fallback — one approval-gated draft per
                    # replying prospect, and none at all when their reply was an explicit no.
                    _route_replied_catchup_to_funnel(user_id, f)
                continue
            if str(f["event_type"]) == NURTURE_EVENT_TYPE:
                # A nurture re-check with no new reply. Nothing to send: the drafted message is the
                # operator's to approve, and nurture NEVER auto-sends a template.
                mark_followup(f["id"], "stopped")
                continue
            msg = build_dm_from_template(user_id, f["event_type"], f["first_name"], my_profile, step=f["next_step"])
            if not msg:
                mark_followup(f["id"], "stopped")
                continue
            send_private_dm.apply_async(kwargs={"user_id": user_id, "profile_url": f["profile_url"], "message": msg})
            insert_new_log(user_id=user_id, action_type=LogActionType.FOLLOWUP, result=LogResultType.SUCCESS,
                           post_url=f["profile_url"], message=msg)
            mark_followup(f["id"], "sent")
            sent += 1
            enqueue_next_followup(user_id, f["profile_url"], f["first_name"], f["event_type"], f["next_step"])
            time.sleep(random.uniform(5, 12))
    finally:
        quit_gracefully(driver)
    return (f"Sent {sent} follow-up(s); drafted {nurtured} nurture reply(ies); "
            f"skipped {skipped} unreadable thread(s)")


def _appreciation_dm_budget(user_id: int) -> int:
    """How many appreciation DMs one pass may dispatch — the SAME per-day DM budget every other DM
    lane spends (`send_scheduled_dm`, the outreach funnel), plus the #626 account envelope.

    The standing-list sources are what make this necessary (issue #968): a month of un-thanked
    mentions is not one DM, it is a burst of them, and the first pass after the flag is flipped
    would otherwise dispatch every one at once. Whoever is left over is never claimed, so the next
    pass thanks them.
    """
    prefs = get_engagement_preferences(user_id)
    return max(0, remaining_actions(user_id, ACTION_DM, int(prefs.get("max_dms_per_day") or 0),
                                    count_dms_sent_today(user_id),
                                    caps=engagement_caps_from_prefs(prefs)))


def _dispatch_appreciation_dms(user_id: int, my_profile: LinkedInProfile, event_type: str,
                               recipients: dict, budget: "int | None" = None) -> int:
    """Send the step-0 appreciation DM for one trigger event to everyone it fired for, and put each
    thread on the follow-up sequencer. Returns how many were dispatched. A missing template used to
    drop the recipient in silence — now it says so, because a template gap here is the difference
    between a nurture pipeline and an empty one (issue #623).

    Every recipient is CLAIMED in `appreciation_touches` (issue #968). The recommendation and
    collaboration sources read standing LinkedIn surfaces and this beat re-queues itself every ~60s,
    so without the claim the same person is thanked on every pass. Already thanked is the normal
    steady state, not a fault — it logs at DEBUG, and it is checked BEFORE the message is written so
    a repeat costs no LLM call. The claim itself lands after the write and before the send, so a
    missing template never burns a person's one shot at being thanked.

    `budget` is the day's remaining DM allowance (`_appreciation_dm_budget`); once it is spent the
    rest of the list is LEFT UNCLAIMED and thanked on a later pass. None means unbounded, which is
    only for callers that have already bounded themselves.
    """
    sent = 0
    for profile_url, name in (recipients or {}).items():
        if budget is not None and sent >= budget:
            log_info(f"Daily DM budget spent — deferring the remaining '{event_type}' "
                     f"appreciation DMs to a later pass", user_id=user_id, action_type="dm")
            break
        first_name = clean_person_name(name).split(" ")[0] or "there"
        if has_appreciation_touch(user_id, profile_url, event_type):
            log_debug(f"Already appreciated {first_name} for '{event_type}' — skipping",
                      user_id=user_id, action_type="dm")
            continue
        message = build_dm_from_template(user_id, event_type, first_name, my_profile)
        if not message:
            log_warning(f"No '{event_type}' DM template for step 0 — skipping the appreciation DM "
                        f"to {first_name}", user_id=user_id, action_type="dm")
            continue
        if not claim_appreciation_touch(user_id, profile_url, event_type, person_name=name):
            # A concurrent pass got there first (or the ledger is unreadable) — either way the safe
            # answer is not to send.
            log_debug(f"Appreciation claim for {first_name} not granted — skipping",
                      user_id=user_id, action_type="dm")
            continue
        send_private_dm.apply_async(kwargs={"user_id": user_id, "profile_url": profile_url,
                                            "message": message})
        enqueue_next_followup(user_id, profile_url, first_name, event_type, 0)
        sent += 1
    return sent


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  reject_on_worker_lost=True, rate_limit='2/m', queue='se_outreach')
def automate_appreciation_dms_for_user(self, user_id: int, loop_for_duration: int = None, future_forward: int = 60):
    """Thank whoever just did something for this account: accepted an invite, recommended, collaborated.

    ONE shared budget spans all three sources (issue #968). The two standing-list sources can each
    hand back a month of people at once, so the first pass after the flag is switched on would
    otherwise go out as a burst LinkedIn reads as a campaign; when the budget is spent the source
    scans are skipped entirely rather than scraped and discarded.

    `loop_for_duration` makes the task re-queue ITSELF `future_forward` seconds out with the
    remaining time, until that runs out — so one dispatch covers a window rather than an instant.
    Every failure is caught and returned as a message string: this beat never fails the worker, and
    `appreciation_touches` is what stops the ~60s re-queue thanking the same person twice.
    """
    user_email, user_password = get_user_password_pair_by_id(user_id)

    driver, wait = get_driver_wait_pair(session_name='Appreciation DMs', user_id=user_id)

    try:
        login_to_linkedin(driver, wait, user_email, user_password)

        start_time = datetime.now()

        log_info("Sending Appreciations here...")

        result = "Appreciation DMs Sent"

        my_profile = load_profile_for_user(user_id)  # cached DB read — only supplies {headline}

        # Every appreciation DM spends the account's ordinary per-day DM budget (issue #968). The
        # two standing-list sources below can each hand back a month of people at once, so without
        # this the first pass after the flag is flipped is a burst LinkedIn reads as a campaign.
        budget = _appreciation_dm_budget(user_id)

        # After Accepting a Connection Request:
        invitations_accepted = accept_connection_request(user_id)
        budget -= _dispatch_appreciation_dms(user_id, my_profile, "connection_accepted",
                                             invitations_accepted, budget)

        if budget <= 0:
            # Scraping a standing list we cannot act on is two page loads for nothing.
            log_debug("Daily DM budget spent — skipping the appreciation source scans",
                      user_id=user_id, action_type="dm")
        else:
            # After Receiving a Recommendation — thank the recommender
            own_profile_url = str(getattr(my_profile, "profile_url", "") or "")
            budget -= _dispatch_appreciation_dms(
                user_id, my_profile, "recommendation_received",
                get_recent_recommendations(driver, wait, user_id, own_profile_url), budget)

        if budget > 0:
            # After a Successful Collaboration — express gratitude and offer to connect further
            budget -= _dispatch_appreciation_dms(
                user_id, my_profile, "collaboration",
                get_recent_collaborators(driver, wait, user_id), budget)

        # Re-schedule the task in the queue for the future
        if loop_for_duration:
            elapsed_time = datetime.now() - start_time
            new_loop_for_duration = round(loop_for_duration - elapsed_time.total_seconds() - future_forward)
            frame = inspect.currentframe()
            current_function_name = frame.f_code.co_name
            args, _, _, values = inspect.getargvalues(frame)
            kwargs = {arg: values[arg] for arg in args}
            # myprint(f"{current_function_name} parameters: {kwargs}")

            if new_loop_for_duration < 0:
                log_info(f"Loop duration reached. Stopping {current_function_name} task...")
            else:
                # Change the value of the loop_for_duration parameter
                kwargs['loop_for_duration'] = new_loop_for_duration
                # Add our function call back to the task queue
                log_info(f"Adding {current_function_name} back to queue for {future_forward} seconds in the future...")
                # Remove 'self' from kwargs if it exists
                if 'self' in kwargs:
                    del kwargs['self']
                # Call self again in the future
                globals()[current_function_name].apply_async(kwargs=kwargs, countdown=future_forward)

    except Exception as e:
        log_error("Error while sending appreciation DMs", exc=e, user_id=user_id, task_name="automate_appreciation_dms_for_user", action_type="dm")
        result = f"Error while sending appreciation DMs: {e}"
    finally:
        quit_gracefully(driver)

    return result


def generate_and_post_comment(driver, wait, post_link, my_profile: LinkedInProfile,
                              profile_synthesis: str = None) -> bool:
    """Write a comment for the post at `post_link` and QUEUE it — this function does not post.

    The text is generated here and handed to the `comment_on_post` task, which opens its own session
    and submits it (issue #966), so a True return means a comment was queued, NOT that one landed on
    LinkedIn. The caller (profile-viewer engagement) uses it only to decide it has engaged with this
    person and can stop walking their activity.

    Returns False for every ordinary reason to skip — already commented, no readable post text, or
    nothing cleared the #617 quality contract — so a False is not an error and is never logged as
    one. Missing engagement preferences or comment history degrade the generation rather than
    stopping it: the account still comments, just without the voice settings or similarity gate.
    """
    if post_link != driver.current_url:
        # Switch to post url
        driver.get(post_link)

    # Get my user_id
    user_id = get_user_id(my_profile.email)

    # Check to make sure user hasn't already commented on this post
    if check_commented(driver, wait, user_id, post_link, my_profile=my_profile):
        log_info("Already commented on this post. Skipping...")
        return False  # Skip posts we've already commented on
    else:
        log_info("Haven't commented on this post yet. Proceeding...")

    try:
        # Get the post content (text) if available
        content = getText(
            get_element_wait_retry(driver, wait,
                                   '//div[contains(@class,"fie-impression-container")]//div[contains(@class,"feed-shared-inline-show-more-text")]',
                                   "Finding Post Text"))
    except Exception as wde:
        log_warning("Failed to get post content, skipping", exc=wde)
        return False  # Skip posts without content

    img_url = None

    # Get the image of post if available (Will not retry)
    img_element = get_element_wait_retry(driver, wait,
                                         '//div[contains(@class,"fie-impression-container")]//div[contains(@class,"update-components-image")]//img',
                                         'Finding Post Image', max_try=0, element_always_expected=False)
    if img_element:
        img_url = img_element.get_attribute('src')

    # NOTE: There is no read more button on full post url page
    # Click the "Read More" Button if exist
    # click_element_wait_retry(driver, wait, '//button[contains(@class, "see-more")]', "Clicking Read More Button",
    #                         parent_element=post['element'], max_try=0, element_always_expected=False)

    # Read-time-realistic delay before engaging (issue #626) — same floor/ceiling as the feed walk,
    # so the permalink path can't be the fast one that gives the account away.
    read_time = pace_read(content, user_id=user_id)
    log_info(f"Simulated Reading... for {read_time:.0f} seconds")

    # Generate AI response — pass the user's engagement preferences so this path honors
    # tone/comment_length/style/emoji/hashtag settings exactly like the feed-commenting path
    # (it previously generated with NO prefs, silently ignoring the user's voice settings).
    try:
        prefs = get_engagement_preferences(user_id)
    except Exception as e:
        log_warning("Could not load engagement preferences for comment; generating with defaults",
                    exc=e, user_id=user_id, action_type="comment")
        prefs = None
    try:
        recent_comments = list(get_recent_comment_texts(user_id))
    except Exception as e:
        log_warning("Could not load recent comment history; similarity gate degrades to none",
                    exc=e, user_id=user_id, action_type="comment")
        recent_comments = []
    with llm_attribution(user_id=user_id, feature=FEATURE_COMMENT):
        comment_text = generate_ai_response(content, my_profile, img_url, prefs=prefs,
                                            profile_synthesis=profile_synthesis,
                                            recent_comments=recent_comments, user_id=user_id)

    if not comment_text:
        log_info("No comment cleared the quality contract for this post — skipping",
                 user_id=user_id, action_type="comment")
        return False

    log_info(f"AI Generated Comment: {comment_text}")

    # This DOES post: the comment is handed to comment_on_post, which opens its own session and
    # submits it. (The line here used to read "Comment out the actual posting of the comment for
    # now" — a leftover from when the apply_async below was commented out, issue #966.)
    kwargs = {'user_id': get_user_id(my_profile.email),
              'post_link': post_link,
              'comment_text': comment_text}
    comment_on_post.apply_async(kwargs=kwargs)

    log_info(f"Comment queued for posting on: {post_link}")

    return True


# The profile-views analytics page is SDUI (grounded live 2026-08-03): no <ul>/<li> list
# container, hashed class names only. A viewer row is an /in/ anchor whose text carries a
# "Viewed …" caption line — a pure TEXT discriminator, per the fix invariants (never classes).
# One JS read returns every row's (href, name, caption) in one shot, so nothing goes stale
# while the list re-renders under the walk.
_PROFILE_VIEWER_ROWS_JS = """
const rows = [...document.querySelectorAll('a[href*="/in/"]')].filter(a =>
  /(^|\\n)Viewed /.test(a.innerText || ''));
return rows.map(a => {
  const lines = (a.innerText || '').split('\\n').map(t => t.trim()).filter(Boolean);
  return {href: a.href, name: lines[0] || '',
          viewed: lines.find(t => t.startsWith('Viewed ')) || ''};
});
"""

# The page's "N Profile viewers" headline (past-90-days stat). A non-zero stat with zero
# matched rows is selector drift, not an empty page — the tripwire that keeps a dead locator
# from reading exactly like nobody-viewed (the #964 failure mode).
_PROFILE_VIEWER_STAT_JS = """
const m = (document.body.innerText || '').match(/([\\d,]+)\\s*\\n\\s*Profile viewers/i);
return m ? parseInt(m[1].replace(/,/g, ''), 10) : null;
"""

# Window scrollTo alone does not grow this list — scrolling the LAST viewer row into view is
# what triggers the lazy loader (grounded live: 8 -> 58 rows this way, scrollTo-only stayed at 8).
_PROFILE_VIEWER_SCROLL_JS = """
const rows = [...document.querySelectorAll('a[href*="/in/"]')].filter(a =>
  /(^|\\n)Viewed /.test(a.innerText || ''));
if (rows.length) rows[rows.length - 1].scrollIntoView({block: 'end'});
window.scrollTo(0, document.body.scrollHeight);
"""


# How long to keep polling an empty viewers list before believing it — the SDUI page paints
# asynchronously and an early read sees zero rows on a page that is still loading.
_PROFILE_VIEWER_RENDER_WAIT_SECONDS = 30


def _viewer_within_lookback(viewed_on: str, lookback_days: int) -> Optional[bool]:
    """Whether a viewed-on caption falls inside the lookback window; None when it won't parse
    (the caller decides whether that stops the walk or just drops the row).
    """
    try:
        viewed_date = convert_viewed_on_to_date(viewed_on)
    except (ValueError, TypeError, AttributeError):
        return None
    return (datetime.now() - viewed_date).days <= lookback_days


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_outreach')
def automate_profile_viewer_engagement(self, user_id: int, loop_for_duration: int = None, future_forward: int = 60,
                                       lookback_days: int = 1):
    """Walk the profile-views analytics list and queue engagement for each viewer inside
    `lookback_days`. The default matches the daily cadence; a catch-up run passes a larger
    window once, then the cadence sticks to the delta.
    """
    log_info("Starting Profile Viewer DMs")

    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Profile Viewer DMs")
    except Exception as e:
        log_error(
            "Failed to get profile for profile viewer engagement",
            exc=e, user_id=user_id, task_name="automate_profile_viewer_engagement",
        )
        return f"Failed to start profile viewer engagement: {e}"

    result = "Profile Viewer DMs Started"

    try:

        # Navigate to profile view page
        driver.get("https://www.linkedin.com/analytics/profile-views/")

        start_time = datetime.now()

        rows: list[dict] = []
        previous_count = -1
        render_deadline = datetime.now() + timedelta(seconds=_PROFILE_VIEWER_RENDER_WAIT_SECONDS)

        while True:  # Keep walking until the last row's viewed-on date falls out of the lookback
            rows = driver.execute_script(_PROFILE_VIEWER_ROWS_JS) or []

            if not rows:
                # The SDUI page renders asynchronously — give the first paint time to land
                # before concluding the list is empty.
                if datetime.now() < render_deadline:
                    time.sleep(2)
                    continue
                break

            # An unparseable last caption means we can't tell whether to keep walking — stop
            # with what we have rather than failing the task through the outer handler.
            last_verdict = _viewer_within_lookback(rows[-1].get('viewed', ''), lookback_days)
            if last_verdict is None:
                log_warning("Could not parse the last profile viewer's viewed-on caption — "
                            "stopping the walk", user_id=user_id,
                            task_name="automate_profile_viewer_engagement")
                break
            if last_verdict is False:
                break  # Walked past the lookback window

            # The list only grows by scrolling. When a scroll adds nothing there is no more to
            # walk, and every row we have is still in range — without this the loop would spin
            # forever on an account whose viewers all fit on one screen.
            if len(rows) <= previous_count:
                log_info("Profile viewers list stopped growing. Ending the walk...")
                break
            previous_count = len(rows)

            driver.execute_script(_PROFILE_VIEWER_SCROLL_JS)
            time.sleep(2)

        if not rows:
            try:
                headline_stat = driver.execute_script(_PROFILE_VIEWER_STAT_JS)
            except WebDriverException:
                headline_stat = None
            if headline_stat:
                log_warning(f"Profile viewers page reports {headline_stat} viewers but the row "
                            f"locator matched none — selector drift", user_id=user_id,
                            task_name="automate_profile_viewer_engagement")
            else:
                # Nobody viewed the profile — nothing to do, not a task failure, so it must
                # not page the error cron.
                log_debug("No profile viewers found on the analytics page — nothing to do",
                          user_id=user_id, task_name="automate_profile_viewer_engagement")

        log_info(f"Final Viewers count: {len(rows)}")

        # Filter per row against the lookback. The walk stops ON an out-of-range viewer, so that
        # row is always in this list — an all-or-nothing filter that threw on one unreadable
        # caption would leave it in and DM someone who viewed weeks ago. A row whose date we
        # cannot read is dropped for the same reason. Keyed on href: it is the viewer's identity
        # (two viewers can share a display name), and it dedupes re-rendered rows.
        viewer_data: dict[str, str] = {}
        for row in rows:
            viewer_url = row.get('href') or ''
            if '/in/' not in viewer_url:
                continue
            verdict = _viewer_within_lookback(row.get('viewed', ''), lookback_days)
            if verdict is None:
                log_warning("Skipping profile viewer with an unreadable viewed-on caption",
                            user_id=user_id, task_name="automate_profile_viewer_engagement")
                continue
            if verdict:
                viewer_data[viewer_url] = row.get('name') or 'LinkedIn Member'

        log_info(f"Filtered Viewers count: {len(viewer_data)}")

        # engage_with_profile_viewer opens its own session and navigates to the profile itself —
        # visiting each viewer here too doubled the profile visits and held this session open
        # for nothing, so the walk just dispatches.
        for viewer_url, viewer_name in viewer_data.items():
            log_info(f"Viewer Name: {viewer_name}")
            log_info(f"Viewer URL: {viewer_url}")

            kwargs = {'user_id': get_user_id(my_profile.email),
                      'viewer_url': viewer_url,
                      'viewer_name': viewer_name}
            engage_with_profile_viewer.apply_async(kwargs=kwargs)

        result = f"Profile Viewer DMs Completed. Engaged with {len(viewer_data)} viewers"

        # Re-schedule the task in the queue for the future
        if loop_for_duration:
            elapsed_time = datetime.now() - start_time
            new_loop_for_duration = round(loop_for_duration - elapsed_time.total_seconds() - future_forward)
            frame = inspect.currentframe()
            current_function_name = frame.f_code.co_name
            args, _, _, values = inspect.getargvalues(frame)
            kwargs = {arg: values[arg] for arg in args}
            # myprint(f"{current_function_name} parameters: {kwargs}")

            if new_loop_for_duration < 0:
                log_info(f"Loop duration reached. Stopping {current_function_name} task...")
            else:
                # Change the value of the loop_for_duration parameter
                kwargs['loop_for_duration'] = new_loop_for_duration
                # Add our function call back to the task queue
                log_info(f"Adding {current_function_name} back to queue for {future_forward} seconds in the future...")
                # Remove 'self' from kwargs if it exists
                if 'self' in kwargs:
                    del kwargs['self']

                # Call self again in the future
                globals()[current_function_name].apply_async(kwargs=kwargs, countdown=future_forward)
    except Exception as e:
        log_error("Error while engaging with profile viewers", exc=e, user_id=user_id, task_name="automate_profile_viewer_engagement")
        result = f"Error while engaging with profile viewers: {e}"
    finally:
        quit_gracefully(driver)

    return result


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'keys': ['user_id', 'viewer_url']},
                  reject_on_worker_lost=True, rate_limit='2/m', queue='se_outreach')
def engage_with_profile_viewer(self, user_id: int, viewer_url, viewer_name):
    """Engage ONE person who viewed the user's profile, branching on whether we are already connected.

    A 1st-degree viewer gets a comment on ONE recent activity (at most one, then the walk stops) and
    a templated DM; anyone else gets a personalised connection request instead — you cannot DM a
    stranger, so the two branches are different actions, not the same action at different depths.

    Bounded to once per viewer per day by `has_engaged_url_with_x_days`, because the analytics page
    lists the same viewer on consecutive runs. Once the attempt actually starts, an ENGAGED row is
    written in `finally` whichever way it goes — a failed engagement is a recorded attempt, with
    SUCCESS/FAILURE saying which, never a gap in the record.
    """
    log_info("Starting Profile Viewer Engagement")

    result = "Profile Viewer Engagement Started"
    engagement_successful = False

    # Check if we already engaged with this viewer today
    if has_engaged_url_with_x_days(user_id, viewer_url, 1):
        log_info(f"Already engaged with {viewer_name} today. Skipping...")
        result = f"Already engaged with {viewer_name} today. Skipping..."
    else:

        try:
            driver, wait, user_email, my_profile = get_current_profile(user_id=user_id,
                                                                       session_name="Profile Viewer Engagement")
        except Exception as e:
            log_error("Error while getting profile for profile viewer engagement", exc=e, user_id=user_id, task_name="engage_with_profile_viewer")
            return f"Failed to start profile viewer engagement: {e}"

        try:

            log_info(f"Engaging from: {my_profile.full_name} to: {viewer_name}")

            if viewer_url != driver.current_url:
                # Switch to viewer_url
                driver.get(viewer_url)

            profile_data = get_linkedin_profile_from_url(driver, wait, viewer_url)
            if profile_data:
                profile = LinkedInProfile(**profile_data)
                # message = profile.generate_personalized_message()
                # myprint(message)

                if profile.is_1st_connection:
                    log_info("We Are 1st Connections")
                    # engage with their content (
                    recent_activities = profile.recent_activities

                    log_info(f"Recent Activities Count: {len(recent_activities)}")

                    # Filter activities by posted date less than a week ago
                    recent_activities = [activity for activity in recent_activities if
                                         (datetime.now() - activity.posted).days <= 7]

                    log_info(f"Recent Activities Filtered (1 week) Count: {len(recent_activities)}")

                    # DONT: Shuffle the activities (they are already in order of latest to oldest)
                    # random.shuffle(recent_activities)
                    able_to_comment = False
                    # Our own stable voice synthesis (the comment is written in the USER's voice).
                    profile_synthesis = get_or_create_profile_synthesis(user_id, my_profile)

                    # Filter list to activities I haven't commented on
                    for activity in recent_activities:
                        link = str(activity.link)

                        # Leave comment on that activity
                        able_to_comment = generate_and_post_comment(driver, wait, link, my_profile,
                                                                    profile_synthesis=profile_synthesis)
                        if able_to_comment:
                            break  # Only comment/interact with one

                    if not able_to_comment:
                        log_info("No activities, unable to or already left comment")

                        first_name = clean_person_name(viewer_name).split(" ")[0] or "there"
                        profile_url_str = str(profile.profile_url)
                        acting_user_id = get_user_id(my_profile.email)

                        # Retrieve past DM history with this profile to avoid repeating messages
                        past_dms = get_dm_history_for_profile(acting_user_id, profile_url_str)
                        message_history_json = json.dumps(past_dms)

                        # Use user's blog URL to personalise the focus when available
                        blog_url = get_user_blog_url(acting_user_id)
                        if blog_url:
                            main_focus = f"Offer insights from my blog at {blog_url} and invite the viewer to discuss topics relevant to their work."
                        else:
                            main_focus = "Offer something of value—insights, resources, or potential collaboration—to the viewer and start a genuine conversation."

                        message = build_dm_from_template(acting_user_id, "profile_viewer", first_name,
                                                         my_profile, blog_url=blog_url or "")

                        # Skip if an equivalent message has already been sent to this person
                        if message:
                            message = ai_check_message_history(message_history_json, main_focus, message, user_name=first_name)


                        if message:

                            # Send actual DM
                            kwargs = {'user_id': acting_user_id,
                                      'profile_url': profile_url_str,
                                      'message': message}
                            send_private_dm.apply_async(kwargs=kwargs)
                            enqueue_next_followup(acting_user_id, profile_url_str, first_name, "profile_viewer", 0)
                            result = f"Profile Viewer Engagement Completed. Sent DM to {viewer_name}"
                            engagement_successful = True
                        else:
                            result = f"Message already sent to {viewer_name}"
                else:
                    # myprint(f"We Are {profile.connection} Connections")
                    # If not 1st connections, send them a connection request
                    # Mention something specific about their profile or company to show genuine interest and that you've done your research
                    recent_activity_summary = summarize_recent_activity(profile, my_profile)
                    response = profile.generate_personalized_message(recent_activity_message=recent_activity_summary,
                                                                     from_name=my_profile.full_name)
                    log_info(f"Original Response: {response}")
                    refined_response = get_ai_message_refinement(response)
                    log_info(f"Refined Response: {refined_response}")

                    # Send connection request with this message
                    kwargs = {'user_id': get_user_id(my_profile.email),
                              'profile_url': str(profile.profile_url),
                              'message': refined_response}
                    invite_to_connect.apply_async(kwargs=kwargs)
                    result = f"Profile Viewer Engagement Completed. Sent Connection Request to {viewer_name}"
                    engagement_successful = True
            else:
                log_info(f"Failed to get profile data for {viewer_name}")
                result = f"Failed to get profile data for {viewer_name}"

        except Exception as e:
            log_error("Error while engaging with profile viewer", exc=e, user_id=user_id, action_type="profile_engagement")
            result = f"Error while engaging with profile viewer: {e}"
        finally:
            # Log engagement efforts to the log
            insert_new_log(user_id=user_id, action_type=LogActionType.ENGAGED,
                           result=LogResultType.SUCCESS if engagement_successful else LogResultType.FAILURE,
                           post_url=viewer_url,
                           message=f"Engaged with {viewer_name}")

            quit_gracefully(driver)

    return result


DM_SEND_CONFIRM_SECONDS = float(os.getenv("DM_SEND_CONFIRM_SECONDS", "6"))

_DM_COMPOSER_XPATH = '//div[contains(@class,"contenteditable")]//p'
# How many times the type step re-finds a composer that went stale under it.
DM_COMPOSER_ATTEMPTS = int(os.getenv("DM_COMPOSER_ATTEMPTS", "3"))
_DM_COMPOSER_SETTLE_SECONDS = 1.5


def _type_dm_into_composer(driver: WebDriver, wait, message: str) -> None:
    """Clear the composer and type the message, re-finding the box on every attempt.

    An element handle is NOT safe to hold across interactions here: the compose overlay re-mounts
    while LinkedIn hydrates it, so the box found the moment it first appears goes stale a keystroke
    later — `StaleElementReferenceException` on the very next `send_keys` is what killed every send
    once the composer itself started resolving. Each attempt therefore re-finds the box, and because
    each one CLEARS before typing, a retry after a half-typed message can never send a doubled one.
    """
    attempts = max(1, DM_COMPOSER_ATTEMPTS)
    for attempt in range(attempts):
        try:
            box = get_element_wait_retry(driver, wait, _DM_COMPOSER_XPATH, 'Finding Message Box',
                                         max_try=1)
            if box is None:
                raise NoSuchElementException("no message composer to type into")
            # Select All then Delete — `clear()` does not work on a contenteditable.
            box.send_keys(Keys.CONTROL + "a")
            box.send_keys(Keys.DELETE)
            simulate_typing(driver, box, message)
            return
        except StaleElementReferenceException:
            # Out of attempts: raise rather than clicking Send over a partial composer.
            if attempt == attempts - 1:
                raise
            log_debug(f"Message composer went stale while typing (attempt {attempt + 1})",
                      action_type="dm")
            time.sleep(_DM_COMPOSER_SETTLE_SECONDS)


def _dm_send_landed(driver: WebDriver, message: str, user_id: int = None,
                    profile_url: str = "") -> bool:
    """Did the message actually POST, or did we just click something? (issue #1030)

    A click that lands is not a message that sent — the whole reason this lane went unnoticed is that
    `dm_sent = True` was written the moment the Send button accepted a click. Confirmation is the
    OUTCOME: our text is the newest message in the thread. A composer that still holds the full text
    is a positive DISPROOF and fails. Anything unreadable falls back to trusting the click (the #875
    pattern) and warns, because reporting a delivered message as failed invites a duplicate send.
    """
    head = " ".join((message or "").split())[:60]
    deadline = time.monotonic() + max(0.0, DM_SEND_CONFIRM_SECONDS)
    while True:
        last = " ".join((read_last_message(driver) or "").split())
        if head and head in last:
            return True
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)

    try:
        still_typed = any(head and head in " ".join((el.text or "").split())
                          for el in driver.find_elements(
                              By.CSS_SELECTOR, "div.msg-form__contenteditable")
                          if el.is_displayed())
    except WebDriverException:
        still_typed = False
    if still_typed:
        log_warning(f"DM never left the composer for {profile_url}", user_id=user_id,
                    action_type="dm")
        return False

    log_warning(f"Could not confirm the DM landed for {profile_url}; trusting the send click",
                user_id=user_id, action_type="dm")
    return True


def send_dm_now(user_id: int, profile_url: str, message: str, person_name: str = None) -> bool:
    """Core DM send: open a composer addressed to this person, type + send a DM (must be a 1st-degree
    connection), log the result. Returns True on success. Shared by send_private_dm (trigger-driven),
    send_scheduled_dm (issue #306 scheduler), the appreciation lane and catch-up congratulations, so
    all four use the same send + logging path — and all four broke together when this one did.

    The entry point is `open_addressed_composer`, NOT a Message control on the profile: LinkedIn now
    renders that affordance as an `<a href='/messaging/compose/…'>` and the old
    `button[aria-label*='Message']` matched nothing, so every DM this function was asked to send
    failed at the first step (issue #1030). Navigating to the person's own compose URL also gives the
    send path something a click never had — a recipient it can read back and verify before typing.
    """
    user_email, user_password = get_user_password_pair_by_id(user_id)

    driver, wait = get_driver_wait_pair(session_name='Private DM', user_id=user_id)

    login_to_linkedin(driver, wait, user_email, user_password)

    dm_sent = False

    log_info("Sending DM: " + message)

    try:
        composer = open_addressed_composer(driver, wait, profile_url, person_name=person_name,
                                           user_id=user_id)
        if not composer.addressed:
            # DEBUG, not a warning: `open_addressed_composer` already warned where it DETECTED the
            # fault, and restating it here would file a second grouped defect for one lost DM (the
            # #1038 wrapper rule). The FAILURE log row below is this lane's record.
            log_debug(f"No composer addressed to {profile_url} ({composer.reason}); not sending",
                      user_id=user_id, action_type="dm")
        else:
            _type_dm_into_composer(driver, wait, message)

            # Sleep so send button can become active
            time.sleep(2)

            # Click the send button
            click_element_wait_retry(driver, wait,
                                     "//button[contains(@class,'msg-form__send-button')]",
                                     "Finding Send Button", max_retry=1, use_action_chain=True)

            dm_sent = _dm_send_landed(driver, message, user_id=user_id, profile_url=profile_url)

    except Exception as e:
        # ERROR, not myprint: this lane failed silently for weeks because every send logged its
        # failure at INFO, which never reaches PostHog and so never escalated (issue #1030).
        log_error("DM send failed", exc=e, user_id=user_id, action_type="dm")

    finally:
        # Update DB logs with DM Sent
        insert_new_log(user_id=user_id, action_type=LogActionType.DM,
                       result=LogResultType.SUCCESS if dm_sent else LogResultType.FAILURE,
                       post_url=profile_url, message=message)
        if dm_sent:
            record_action(user_id, ACTION_DM)  # account-level governor (issue #626)

        quit_gracefully(driver)  # Close the driver

    return dm_sent


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True}, reject_on_worker_lost=True,
                  rate_limit='2/m', queue='se_outreach')
def send_private_dm(self, user_id: int, profile_url: str, message: str):
    """Send dm message to a profile. Must be a 1st connection"""
    dm_sent = send_dm_now(user_id, profile_url, message)
    result = "DM Sent Successfully" if dm_sent else "DM Failed"
    log_info(result)
    return result


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'keys': ['dm_id']},
                  reject_on_worker_lost=True, rate_limit='2/m', queue='se_outreach')
def send_scheduled_dm(self, dm_id: int):
    """Send a scheduled 1:1 DM (issue #306). Enforces the per-day DM cap at send time (defers back
    to 'approved' for the next scan when the cap is hit) and updates the scheduled_dms status.
    """
    from cqc_lem.utilities.db import (
        ScheduledDmStatus,
        count_dms_sent_today,
        get_scheduled_dm,
        update_scheduled_dm_status,
    )
    dm = get_scheduled_dm(dm_id)
    if not dm or dm["status"] not in (ScheduledDmStatus.APPROVED, ScheduledDmStatus.SCHEDULED):
        return f"Scheduled DM {dm_id} not sendable (status={dm['status'] if dm else 'missing'})"

    user_id = dm["user_id"]
    prefs = get_engagement_preferences(user_id)
    if remaining_actions(user_id, ACTION_DM, int(prefs.get("max_dms_per_day") or 0),
                         count_dms_sent_today(user_id),
                         caps=engagement_caps_from_prefs(prefs)) <= 0:
        log_info(f"send_scheduled_dm: daily DM budget spent for user {user_id}; deferring DM {dm_id}")
        update_scheduled_dm_status(dm_id, ScheduledDmStatus.APPROVED)  # retry on the next scan
        return f"Scheduled DM {dm_id} deferred (daily DM cap reached)"

    dm_sent = send_dm_now(user_id, dm["recipient_profile_url"], dm["message"])
    update_scheduled_dm_status(dm_id, ScheduledDmStatus.SENT if dm_sent else ScheduledDmStatus.FAILED)
    return f"Scheduled DM {dm_id} -> {'sent' if dm_sent else 'failed'}"


def _reply_to_person_on_post(driver, wait, post_url: str, person_profile_url: str, text: str,
                             user_id: int = None) -> bool:
    """Post `text` as a reply UNDER a specific person's comment on `post_url` — the delivery half of
    an approved hot-lead response (issue #483). Reuses the #478 comment-thread helpers: a tall
    viewport + scrolling is what actually makes comments lazy-render on a long post.
    """
    slug = profile_slug(person_profile_url)
    if not slug:
        log_warning("Lead response: no profile slug to target", user_id=user_id, action_type="reply")
        return False
    try:
        driver.set_window_size(1400, 3400)
    except Exception:
        pass  # some drivers reject resize; the scrolling below is the fallback
    driver.get(post_url)
    time.sleep(random.uniform(2.5, 4))
    for _ in range(10):
        driver.execute_script("window.scrollBy(0, 1000);")
        time.sleep(random.uniform(0.9, 1.4))
        cl = driver.find_elements(By.CSS_SELECTOR, "[data-testid*='commentList']")
        if cl:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", cl[0])
    for tb in driver.find_elements(By.CSS_SELECTOR, _COMMENTLIST_TEXTBOX):
        cont = _comment_container(driver, tb)
        if cont is None:
            continue
        if f"/in/{slug}" not in _comment_header_author(driver, cont):
            continue
        return _reply_under_comment_inline(driver, wait, cont, text, user_id=user_id)
    log_warning(f"Lead response: no comment by /in/{slug} found on {post_url}", user_id=user_id,
                action_type="reply")
    return False


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'keys': ['signal_id']},
                  reject_on_worker_lost=True, rate_limit='2/m', queue='se_outreach')
def send_lead_response(self, signal_id: int):
    """Deliver an APPROVED hot-lead response (issue #483) — a reply under their comment, or a DM."""
    return _send_lead_response(signal_id)


def _send_lead_response(signal_id: int) -> str:
    """Body of send_lead_response, extracted for unit testing (no QueueOnce/Redis). Only ever acts
    on a signal a human APPROVED — nothing here can auto-respond to a lead on its own.
    """
    signal = get_lead_signal(signal_id)
    if not signal:
        return f"Lead signal {signal_id} not found"
    if str(signal.get("status")) != str(LeadSignalStatus.APPROVED):
        return f"Lead signal {signal_id} not sendable (status={signal.get('status')})"
    message = (signal.get("draft_response") or "").strip()
    if not message:
        update_lead_signal(signal_id, status=LeadSignalStatus.FAILED)
        return f"Lead signal {signal_id} has no draft to send"

    user_id = signal["user_id"]
    if str(signal.get("channel")) == str(LeadSignalChannel.DM):
        sent = send_dm_now(user_id, signal["person_profile_url"], message)
        update_lead_signal(signal_id, status=LeadSignalStatus.SENT if sent else LeadSignalStatus.FAILED)
        return f"Lead signal {signal_id} DM -> {'sent' if sent else 'failed'}"

    if not signal.get("context_url"):
        update_lead_signal(signal_id, status=LeadSignalStatus.FAILED)
        return f"Lead signal {signal_id} has no post to reply on"
    try:
        driver, wait, _email, _my_profile = get_current_profile(user_id=user_id, session_name="Lead Response")
    except LinkedInRateLimited as e:
        log_warning("Lead response skipped — LinkedIn rate-limited", exc=e, user_id=user_id,
                    task_name="send_lead_response")
        return "Skipped — rate limited"  # left APPROVED so a later run retries it
    except Exception as e:
        log_error("Error starting lead response", exc=e, user_id=user_id, task_name="send_lead_response")
        return f"Failed to start lead response: {e}"
    try:
        sent = _reply_to_person_on_post(driver, wait, signal["context_url"],
                                        signal.get("person_profile_url") or "", message, user_id=user_id)
        update_lead_signal(signal_id, status=LeadSignalStatus.SENT if sent else LeadSignalStatus.FAILED)
        insert_new_log(user_id=user_id, post_id=signal.get("post_id"), action_type=LogActionType.REPLY,
                       result=LogResultType.SUCCESS if sent else LogResultType.FAILURE,
                       post_url=signal["context_url"], message=message)
        return f"Lead signal {signal_id} reply -> {'sent' if sent else 'failed'}"
    finally:
        quit_gracefully(driver)


# --- Smart connection targeting (issue #486) — source warm engagers into #398's send path ---
# Hard per-scan ceiling, independent of the user's daily invite cap: sourcing should trickle in a
# handful of high-fit people a day, never file a queue that looks like list-blasting.
def _connect_env_int(name: str, default: int) -> int:
    """A typo'd env var must not take the worker down at import time — fail safe to the default."""
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        log_warning(f"Invalid {name} env value; falling back to {default}",
                    action_type="connection_targeting")
        return default


_MAX_NEW_CONNECT_TARGETS_PER_SCAN = _connect_env_int("MAX_NEW_CONNECT_TARGETS_PER_SCAN", 5)
_MAX_ADJACENT_AUTHORS_PER_SCAN = 3
_MAX_ADJACENT_POSTS_PER_AUTHOR = 2
_MAX_ENGAGERS_PER_ADJACENT_POST = 15
_CONNECT_ENGAGER_LOOKBACK_DAYS = 30


def _harvest_post_commenters(driver, post_url: str, author_name: str, now: datetime,
                             limit: int = _MAX_ENGAGERS_PER_ADJACENT_POST) -> list:
    """Commenters on ONE post as connection-targeting signals. Reuses the SDUI comment-thread walker
    the reply sweep uses, so it survives the same DOM churn.
    """
    driver.get(post_url)
    time.sleep(random.uniform(3, 5))
    signals = []
    for comment in _comment_items_from_thread(driver)[:limit]:
        try:
            link = comment.find_element(By.CSS_SELECTOR, "a[href*='/in/']")
            raw = (link.text or "") or (link.get_attribute("aria-label") or "")
            name = clean_person_name(raw)
            url = (link.get_attribute("href") or "").split("?")[0]
        except Exception:
            continue
        if not name or not url:
            continue
        signals.append(CandidateSignal(person_name=name, person_profile_url=url,
                                       source=SOURCE_ADJACENT_POST, context_url=post_url,
                                       context_author=author_name, occurred_at=now,
                                       connection_degree=connection_degree(raw)))
    return signals


def _adjacent_author_signals(driver, user_id: int, authors: list, my_name: str,
                             now: datetime) -> list:
    """Harvest engagers from the recent posts of the user's configured adjacent authors (thought
    leaders / competitors). Each author is best-effort: one unreachable profile must not lose the
    others' candidates.
    """
    from cqc_lem.utilities.linkedin.scrapper import get_profile_recent_activity

    signals: list = []
    for author_url in authors[:_MAX_ADJACENT_AUTHORS_PER_SCAN]:
        author_name = _author_display_name(author_url)
        try:
            activity = get_profile_recent_activity(driver, author_url) or []
        except Exception as e:
            log_warning("Could not read adjacent author's recent activity", exc=e, user_id=user_id,
                        action_type="connection_targeting")
            continue
        for post in activity[:_MAX_ADJACENT_POSTS_PER_AUTHOR]:
            link = (post or {}).get("link")
            if not link:
                continue
            try:
                signals.extend(_harvest_post_commenters(driver, link, author_name, now))
            except Exception as e:
                log_warning("Could not harvest engagers from adjacent post", exc=e, user_id=user_id,
                            action_type="connection_targeting")
    me = (my_name or "").strip().lower()
    return [s for s in signals if (s.person_name or "").strip().lower() != me]


def _connect_target_budget(user_id: int, prefs: dict, max_new: int = None) -> int:
    """How many NEW targets this scan may file. The daily invite cap is shared with the reactive
    profile-viewer flow AND with targets already waiting, so sourcing can never build a backlog that
    spends tomorrow's cap the moment it opens.
    """
    cap = int(prefs.get("max_invites_per_day") or 0)
    remaining = cap - count_invites_sent_today(user_id) - count_open_connection_requests(user_id)
    ceiling = _MAX_NEW_CONNECT_TARGETS_PER_SCAN if max_new is None else int(max_new)
    return max(0, min(remaining, ceiling))


def _target_status_for_mode(mode: str, prefs: dict) -> "ConnectionRequestStatus":
    """'suggest' (default) ALWAYS files a draft needing human approval; 'auto_queue' defers to the
    user's #398 connection_request_mode. So enabling targeting alone can never send anything.
    """
    if mode != "auto_queue":
        return ConnectionRequestStatus.PENDING
    return (ConnectionRequestStatus.APPROVED
            if prefs.get("connection_request_mode") == "auto_approve"
            else ConnectionRequestStatus.PENDING)


@shared_task.task(bind=True, base=QueueOnce,
                  once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  reject_on_worker_lost=True, rate_limit='1/m', queue='se_outreach')
def scan_connection_candidates(self, user_id: int, max_new: int = None):
    """Source ICP-fit connection targets from people who engage with content (issue #486).

    Candidates come from the engagers on the user's OWN posts (read from post_engagers — no
    scraping) plus the commenters on the recent posts of the adjacent authors they configured
    (scraped). They're ICP-scored against the user's focus topics, deduped against every
    connection_requests row they've ever had, and filed as #398 requests with a personalized note —
    so the existing approval gate, combined daily invite cap and 429 / kill-switch backoff all still
    apply. Nothing is sent from here.
    """
    prefs = get_engagement_preferences(user_id)
    mode = str(prefs.get("connection_targeting_mode") or "suggest")
    if mode == "off":
        log_info("Connection targeting is off for this user", user_id=user_id,
                 task_name="scan_connection_candidates", action_type="connection_targeting")
        return f"Connection targeting off for user {user_id}"

    budget = _connect_target_budget(user_id, prefs, max_new)
    if budget <= 0:
        # Filing nothing is this scan's resting state, not a degraded path: the cap doing its job,
        # a quiet audience, and a fully-deduped candidate set are all working behaviour. Warning on
        # any of them files a defect for a healthy daily beat (issue #985).
        log_debug("Connection targeting filed nothing: no invite budget left (daily cap already "
                  "spent or fully queued)", user_id=user_id,
                  task_name="scan_connection_candidates", action_type="connection_targeting")
        return f"No invite budget left for user {user_id}"

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    signals = [CandidateSignal(person_name=row.get("person_name"),
                               person_profile_url=row.get("person_profile_url"),
                               source=SOURCE_OWN_POST, occurred_at=row.get("occurred_at"),
                               connection_degree=row.get("connection_degree"))
               for row in get_engager_candidates(user_id, days=_CONNECT_ENGAGER_LOOKBACK_DAYS)]

    authors = [str(a).strip() for a in (prefs.get("connection_target_authors") or []) if str(a or "").strip()]
    if authors:
        driver = None
        try:
            driver, _wait, _user_email, my_profile = get_current_profile(
                user_id=user_id, session_name="Connection Targeting")
            signals.extend(_adjacent_author_signals(driver, user_id, authors,
                                                    my_profile.full_name, now))
        except Exception as e:
            # Own-post engagers still stand on their own — degrade, don't abort the whole scan.
            log_warning("Adjacent-author sourcing failed; using own-post engagers only", exc=e,
                        user_id=user_id, task_name="scan_connection_candidates")
        finally:
            if driver is not None:
                quit_gracefully(driver)

    if not signals:
        log_debug("Connection targeting filed nothing: nobody has engaged with this user's "
                  "content in the lookback window and no adjacent authors are configured",
                  user_id=user_id, task_name="scan_connection_candidates",
                  action_type="connection_targeting")
        return f"No connection candidates found for user {user_id}"

    terms = target_terms_from_prefs(prefs)
    urls = sorted({s.person_profile_url for s in signals if s.person_profile_url})
    min_icp = int(prefs.get("min_connection_icp_score") or 0)
    candidates = rank_candidates(signals, now, facts_by_url=get_profile_facts(urls),
                                 target_terms=terms, min_icp=min_icp,
                                 exclude_keys=get_requested_person_keys(user_id),
                                 limit=budget)
    if not candidates:
        log_debug("Connection targeting filed nothing: every candidate was already requested, "
                  "already a 1st-degree connection, or below the ICP floor", user_id=user_id,
                  task_name="scan_connection_candidates", action_type="connection_targeting")
        return f"No new connection candidates for user {user_id} (all deduped or below ICP floor)"

    status = _target_status_for_mode(mode, prefs)
    topic = terms[0] if terms else None
    filed = 0
    for candidate in candidates:
        # Belt and braces on the two gates that produced the only request production ever filed —
        # an invite to an existing connection, scored below the user's own floor (issue #623).
        if is_first_degree(candidate.connection_degree or ""):
            log_info(f"Skipping {candidate.person_name or candidate.person_profile_url}: already a "
                     f"1st-degree connection", user_id=user_id,
                     task_name="scan_connection_candidates", action_type="connection_targeting")
            continue
        if candidate.known_fit and candidate.icp_score < min_icp:
            log_info(f"Skipping {candidate.person_name or candidate.person_profile_url}: ICP "
                     f"{candidate.icp_score} below the floor of {min_icp}", user_id=user_id,
                     task_name="scan_connection_candidates", action_type="connection_targeting")
            continue
        note = _draft_connect_note(user_id, candidate, topic=topic)
        request_id = insert_connection_request(
            user_id, candidate.person_profile_url, message=note,
            recipient_name=candidate.person_name, status=status,
            source=candidate.source,
            # No facts to score against means no score. Storing ICP_UNKNOWN here filed rows that
            # read as "below your floor" to whoever approves them; `reasons` says fit is unverified.
            icp_score=(candidate.icp_score if candidate.known_fit else None),
            reasons=candidate.reasons)
        if not request_id:
            continue
        filed += 1
        log_info(f"Connection target filed ({candidate.source}, score {candidate.score})",
                 user_id=user_id, task_name="scan_connection_candidates",
                 action_type="connection_targeting")
    return f"Filed {filed} connection target(s) as '{status}' for user {user_id}"


# --- Comment-first outreach funnel (issue #399) — approval-gated comment->connect->DM ---
_FUNNEL_CONNECT_NOTE = ("Hi {first_name}, I've been enjoying your posts and the perspective you "
                        "share — would love to connect and keep in touch.")


def _funnel_first_name(target: dict) -> str:
    name = (target.get("target_name") or "").strip()
    return name.split()[0] if name else "there"


def _draft_funnel_stage(user_id: int, stage: str, target: dict) -> str:
    """Draft the voice-aligned action text for the NEXT funnel stage. Connect notes are refined to
    the user's voice; the DM is rendered from the user's 'funnel' DM template (existing machinery).
    Returns '' when there's nothing to pre-draft — the operator can still edit before approving.
    """
    first_name = _funnel_first_name(target)
    if stage == OutreachStage.CONNECT:
        base = _FUNNEL_CONNECT_NOTE.format(first_name=first_name)
        try:
            return (get_ai_message_refinement(base, character_limit=300) or base).strip()
        except Exception as e:
            log_warning("Funnel connect-note refinement failed", exc=e, user_id=user_id,
                        action_type="outreach_funnel")
            return base
    if stage == OutreachStage.DM:
        # my_profile is only used for the {headline} fallback; None is safe (see build_dm_from_template).
        return (build_dm_from_template(user_id, "funnel", first_name, None) or "").strip()
    return ""


def _fire_funnel_stage(user_id: int, target: dict) -> str:
    """Fire one APPROVED funnel stage by enqueuing the existing primitive, then advance the target to
    the next stage as 'pending' (needs a fresh approval). Daily caps DEFER a stage (leave it approved
    for the next run) rather than auto-firing it.
    """
    target_id = target["id"]
    stage = str(target.get("stage"))
    profile_url = target.get("target_profile_url")
    draft = (target.get("draft_text") or "").strip()
    first_name = _funnel_first_name(target)
    prefs = get_engagement_preferences(user_id)

    if not draft:
        update_outreach_target_status(target_id, OutreachStatus.SKIPPED)
        log_warning("Funnel target approved with no draft text; skipping stage",
                    user_id=user_id, action_type="outreach_funnel")
        return f"target {target_id}: skipped {stage} (no draft)"

    if stage == OutreachStage.COMMENT:
        # Same paced budget + account governor the feed walk spends against (issue #626), so the
        # funnel can't quietly push the day past the account envelope.
        if remaining_actions(user_id, ACTION_COMMENT, int(prefs.get("max_comments_per_day") or 0),
                             count_comments_today(user_id),
                             caps=engagement_caps_from_prefs(prefs)) <= 0:
            return f"target {target_id}: comment deferred (daily comment cap)"
        context_url = target.get("context_url")
        if not context_url:
            update_outreach_target_status(target_id, OutreachStatus.FAILED)
            log_warning("Funnel comment stage has no post URL to comment on",
                        user_id=user_id, action_type="outreach_funnel")
            return f"target {target_id}: comment failed (no post url)"
        comment_on_post.apply_async(kwargs={"user_id": user_id, "post_link": context_url,
                                            "comment_text": draft})
        next_stage = OutreachStage.CONNECT
    elif stage == OutreachStage.CONNECT:
        invite_to_connect.apply_async(kwargs={"user_id": user_id, "profile_url": profile_url,
                                              "message": draft})
        next_stage = OutreachStage.DM
    elif stage == OutreachStage.DM:
        if remaining_actions(user_id, ACTION_DM, int(prefs.get("max_dms_per_day") or 0),
                             count_dms_sent_today(user_id),
                             caps=engagement_caps_from_prefs(prefs)) <= 0:
            return f"target {target_id}: DM deferred (daily DM cap)"
        send_private_dm.apply_async(kwargs={"user_id": user_id, "profile_url": profile_url,
                                            "message": draft})
        enqueue_next_followup(user_id, profile_url, first_name, "funnel", 0)
        update_outreach_target(target_id, stage=OutreachStage.COMPLETED, status=OutreachStatus.ACTED)
        return f"target {target_id}: DM sent -> funnel completed"
    else:
        return f"target {target_id}: nothing to do (stage={stage})"

    next_draft = _draft_funnel_stage(user_id, next_stage, target)
    update_outreach_target(target_id, stage=next_stage, status=OutreachStatus.PENDING,
                           draft_text=next_draft)
    return f"target {target_id}: {stage} fired -> {next_stage} pending approval"


@shared_task.task(bind=True, base=QueueOnce,
                  once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']})
def process_outreach_funnel(self, user_id: int, max_per_run: int = 25):
    """Advance a user's APPROVED comment->connect->DM funnel targets one stage each (issue #399).
    Every stage is approval-gated: only status='approved' rows are acted on, and each fired stage
    drops the target to the NEXT stage as 'pending' — requiring a fresh human approval — so no step
    ever auto-fires at volume. Reuses comment_on_post / invite_to_connect / send_private_dm and the
    DM follow-up machinery; daily comment/DM caps defer a stage rather than fire it.
    """
    targets = get_approved_outreach_targets(user_id)
    if not targets:
        return f"No approved outreach targets for user {user_id}"
    results = []
    for target in targets[:max_per_run]:
        try:
            results.append(_fire_funnel_stage(user_id, target))
        except Exception as e:
            log_error("Outreach funnel stage failed", exc=e, user_id=user_id,
                      action_type="outreach_funnel")
            update_outreach_target_status(target["id"], OutreachStatus.FAILED)
            results.append(f"target {target['id']}: error")
    return "; ".join(results)


# --- Outreach funnel sourcing (issue #623) — the funnel had a processor but no input -------------
# outreach_funnel_targets had ZERO rows in production since the table shipped: the only way in was
# the API, and nothing ever called it. These are the three warm surfaces we already track, drained
# into the SAME approval-gated funnel #399 built. Nothing here sends: every target lands as a draft
# whose first stage still needs approval (or the user's auto-approve mode), and each fired stage
# still drops back to 'pending' for a fresh one.
_MAX_NEW_FUNNEL_TARGETS_PER_SCAN = _connect_env_int("MAX_NEW_FUNNEL_TARGETS_PER_SCAN", 5)
# Depth at which sourcing stops: an approval queue nobody works through is the same as no queue.
_MAX_OPEN_FUNNEL_TARGETS = _connect_env_int("MAX_OPEN_FUNNEL_TARGETS", 25)
_MAX_ROSTER_AUTHORS_PER_FUNNEL_SCAN = 5
_FUNNEL_ENGAGER_LOOKBACK_DAYS = 14


def _funnel_prospects_from_roster(driver, user_id: int, roster: list, my_name: str,
                                  now: datetime) -> list:
    """The engagement roster (G1, issue #616) and the people commenting on its posts, each paired
    with the post that put them on our radar — the comment stage needs something to comment ON.
    Each roster author is best-effort: one unreachable profile must not lose the others.
    """
    from cqc_lem.utilities.linkedin.scrapper import get_profile_recent_activity

    prospects: list = []
    for target in roster[:_MAX_ROSTER_AUTHORS_PER_FUNNEL_SCAN]:
        author_url = str(target.get("profile_url") or "").strip()
        if not author_url:
            continue
        try:
            activity = get_profile_recent_activity(driver, author_url) or []
        except Exception as e:
            log_warning("Could not read a roster author's recent activity", exc=e, user_id=user_id,
                        action_type="outreach_funnel")
            continue
        post = next((p for p in activity if (p or {}).get("link")), None)
        if not post:
            # A roster author who simply hasn't posted lately is the common case, not a degraded
            # one — most people post weekly at best against a daily beat. Warning on it escalates
            # to ERROR after three repeats and files a defect for working behaviour (issue #987,
            # sibling of #985/#995). The real failures around it keep their warnings.
            log_debug(f"Roster author {author_url} has no recent post to comment on — skipping",
                      user_id=user_id, action_type="outreach_funnel")
            continue
        author_name = clean_person_name(target.get("name") or "") or _author_display_name(author_url)
        post_text = str(post.get("text") or "")
        prospects.append({"profile_url": author_url, "name": author_name,
                          "context_url": post["link"], "context_text": post_text,
                          "stage": OutreachStage.COMMENT})
        try:
            commenters = _harvest_post_commenters(driver, post["link"], author_name, now)
        except Exception as e:
            log_warning("Could not harvest commenters from a roster post", exc=e, user_id=user_id,
                        action_type="outreach_funnel")
            continue
        for signal in commenters:
            prospects.append({"profile_url": signal.person_profile_url, "name": signal.person_name,
                              "context_url": post["link"], "context_text": post_text,
                              "stage": OutreachStage.COMMENT})
    me = (my_name or "").strip().lower()
    return [p for p in prospects if (p["name"] or "").strip().lower() != me]


def _funnel_prospects_from_engagers(user_id: int) -> list:
    """People who engaged with the user's OWN posts. They start at the CONNECT stage: they already
    engaged with us, so there is no third-party post to comment on first — and anyone the badge says
    we're already connected to is dropped, since connecting is the only thing this funnel adds.
    """
    prospects: list = []
    for row in get_engager_candidates(user_id, days=_FUNNEL_ENGAGER_LOOKBACK_DAYS):
        if is_first_degree(row.get("connection_degree") or ""):
            continue
        prospects.append({"profile_url": row.get("person_profile_url"),
                          "name": clean_person_name(row.get("person_name") or ""),
                          "context_url": None, "context_text": "",
                          "stage": OutreachStage.CONNECT})
    return prospects


def _draft_funnel_comment(user_id: int, prospect: dict, my_profile: LinkedInProfile,
                          prefs: dict = None, profile_synthesis: str = None) -> str:
    """Pre-draft the comment-stage text from the post itself, using the same grounded generator the
    feed uses (so the quality contract and similarity gate apply). Returns '' when the post text is
    unreadable or the gate rejects every attempt — the operator writes it themselves rather than a
    template comment going out under their name.
    """
    content = (prospect.get("context_text") or "").strip()
    if not content or my_profile is None:
        return ""
    try:
        return (generate_ai_response(content, my_profile, None, prefs=prefs,
                                     profile_synthesis=profile_synthesis, user_id=user_id) or "").strip()
    except Exception as e:
        log_warning("Funnel comment draft failed; leaving it for the operator", exc=e,
                    user_id=user_id, action_type="outreach_funnel")
        return ""


@shared_task.task(bind=True, base=QueueOnce,
                  once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  reject_on_worker_lost=True, rate_limit='1/m', queue='se_outreach')
def scan_outreach_funnel_targets(self, user_id: int, max_new: int = None):
    """Populate the comment-first outreach funnel from the roster, roster-post commenters, and the
    user's own post engagers (issue #623). Files drafts only — the approval gate, the per-stage
    re-approval, and the daily comment/DM caps in process_outreach_funnel all still apply.
    """
    prefs = get_engagement_preferences(user_id)
    if str(prefs.get("connection_targeting_mode") or "suggest") == "off":
        log_info("Outreach sourcing is off for this user (connection_targeting_mode=off)",
                 user_id=user_id, task_name="scan_outreach_funnel_targets",
                 action_type="outreach_funnel")
        return f"Outreach funnel sourcing off for user {user_id}"

    open_targets = count_open_outreach_targets(user_id)
    ceiling = _MAX_NEW_FUNNEL_TARGETS_PER_SCAN if max_new is None else int(max_new)
    budget = max(0, min(ceiling, _MAX_OPEN_FUNNEL_TARGETS - open_targets))
    if budget <= 0:
        # Filing nothing is this scan's resting state, not a degraded path: the backlog gate doing
        # its job, an unconfigured roster, and a quiet audience are all working behaviour. Warning
        # on any of them files a defect for a healthy daily beat (issue #995, sibling of #985).
        log_debug(f"Outreach funnel sourcing filed nothing: {open_targets} target(s) are already "
                  f"waiting for approval", user_id=user_id,
                  task_name="scan_outreach_funnel_targets", action_type="outreach_funnel")
        return f"Outreach funnel backlog full for user {user_id}"

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    roster = [t for t in get_engagement_targets(user_id, active_only=True) if t.get("profile_url")]
    prospects: list = []
    my_profile = None
    synthesis = None
    if roster:
        driver = None
        try:
            driver, _wait, _user_email, my_profile = get_current_profile(
                user_id=user_id, session_name="Outreach Funnel Sourcing")
            synthesis = get_or_create_profile_synthesis(user_id, my_profile)
            prospects.extend(_funnel_prospects_from_roster(driver, user_id, roster,
                                                           my_profile.full_name, now))
        except Exception as e:
            # Own-post engagers stand on their own — degrade, don't abort the whole scan.
            log_warning("Roster sourcing for the outreach funnel failed; using post engagers only",
                        exc=e, user_id=user_id, task_name="scan_outreach_funnel_targets",
                        action_type="outreach_funnel")
        finally:
            if driver is not None:
                quit_gracefully(driver)
    else:
        log_debug("No active engagement-roster targets — the funnel can only source from post "
                  "engagers until a roster is configured", user_id=user_id,
                  task_name="scan_outreach_funnel_targets", action_type="outreach_funnel")
    prospects.extend(_funnel_prospects_from_engagers(user_id))

    if not prospects:
        log_debug("Outreach funnel sourcing filed nothing: the engagement roster produced no "
                  "posts and nobody has engaged with this user's content in the lookback window",
                  user_id=user_id, task_name="scan_outreach_funnel_targets",
                  action_type="outreach_funnel")
        return f"No outreach funnel prospects for user {user_id}"

    # A target already in connection_requests is being worked by the #398/#486 path — two outbound
    # tracks aimed at one person is exactly the over-automation this stays gated against.
    requested = get_requested_person_keys(user_id)
    status = _target_status_for_mode("auto_queue", prefs)
    seen: set = set()
    filed = 0
    for prospect in prospects:
        if filed >= budget:
            break
        url = str(prospect.get("profile_url") or "").strip()
        if not url:
            continue
        key = person_key(prospect.get("name"), url)
        if key in seen or key in requested:
            continue
        seen.add(key)
        if get_outreach_target_by_url(user_id, url):
            continue
        stage = prospect["stage"]
        draft = (_draft_funnel_comment(user_id, prospect, my_profile, prefs=prefs,
                                       profile_synthesis=synthesis)
                 if stage == OutreachStage.COMMENT
                 else _draft_funnel_stage(user_id, stage, {"target_name": prospect.get("name")}))
        target_id = insert_outreach_target(user_id, url, target_name=prospect.get("name") or None,
                                           context_url=prospect.get("context_url"),
                                           draft_text=(draft or None), stage=stage, status=status)
        if not target_id:
            continue
        filed += 1
        log_info(f"Outreach funnel target filed at the {stage} stage", user_id=user_id,
                 task_name="scan_outreach_funnel_targets", action_type="outreach_funnel")
    return f"Filed {filed} outreach funnel target(s) as '{status}' for user {user_id}"


# --- LinkedIn Catch-up automation (issue #482) — trigger-event congratulations, approval-gated ---
# The catch-up feed is a daily stream of network "moments". New job / promotion moments are genuine
# trigger events (a reason to reconnect that isn't salesy); birthdays are small talk. So every moment
# is CLASSIFIED, ICP-SCORED against the user's targeting prefs, DEDUPED on the milestone, drafted from
# the user's own DM template, and held for human approval before a single message goes out.
CATCHUP_URL = "https://www.linkedin.com/mynetwork/catch-up/all/"
# Moments scoring below this are recorded as 'skipped' tombstones (dedup) instead of drafted — a
# generic "Congrats!" is worse than saying nothing.
CATCHUP_MIN_SCORE = int(os.getenv("CATCHUP_MIN_SCORE", "30"))

# Why a catch-up run produced what it produced (issue #792). Every run reports one of these — the
# quiet outcomes especially, because "the feed had no milestone today", "the 429 breaker never let
# the scan start" and "the card selectors have drifted" are the SAME silence without them.
CATCHUP_PHASE_SCAN = "scan"
CATCHUP_PHASE_SEND = "send"
CATCHUP_PHASE_DELIVER = "deliver"                   # the per-touch terminal outcome
CATCHUP_STATUS_DRAFTED = "drafted"
CATCHUP_STATUS_DISABLED = "disabled"                # no milestone type enabled for this user
CATCHUP_STATUS_THROTTLED = "throttled"              # 429 breaker / automation pause
CATCHUP_STATUS_SESSION_FAILED = "session_failed"
CATCHUP_STATUS_SCRAPE_FAILED = "scrape_failed"
CATCHUP_STATUS_NO_MOMENTS = "no_moments"            # the feed rendered nothing we could read
CATCHUP_STATUS_NONE_QUALIFIED = "none_qualified"    # moments read, none survived the funnel
CATCHUP_STATUS_DISPATCHED = "dispatched"
CATCHUP_STATUS_NOTHING_TO_SEND = "nothing_to_send"
CATCHUP_STATUS_CAPPED = "capped"                    # approved touches exist, today's cap is spent
CATCHUP_STATUS_INACTIVE = "inactive_users"          # queue exists but its owners aren't connected
CATCHUP_STATUS_AWAITING_APPROVAL = "awaiting_approval"  # drafts exist, none approved yet
# Terminal (deliver) outcomes. `dispatched` is NOT delivery: a touch the DM cap or the breaker defers
# goes back to 'approved' and the drip re-dispatches it on the next beat, so a lane that never sends
# anything reads as a healthy rising `dispatched` count unless the send itself reports.
CATCHUP_STATUS_SENT = "sent"
CATCHUP_STATUS_FAILED = "failed"                    # send_dm_now could not deliver it
CATCHUP_STATUS_DM_CAPPED = "dm_capped"              # the ACCOUNT-wide DM cap, not the catch-up one
CATCHUP_STATUS_NO_MESSAGE = "no_message"            # approved with an empty body
CATCHUP_STATUS_NOT_SENDABLE = "not_sendable"        # row missing or no longer approved/sending
CATCHUP_STATUS_CONTACT_COOLDOWN = "contact_cooldown"  # per-contact interval hasn't elapsed (issue #1078)
CATCHUP_STATUS_CONTACT_CAP = "contact_cap"            # per-contact window cap reached (issue #1078)
CATCHUP_STATUS_ALREADY_SENT = "already_sent"          # durable claim row already exists (issue #1078)

# Ordered fallback chain for the catch-up cards. Live-grounded 2026-08-03 on a real session: the
# surface is full SDUI — every card is a div[role='listitem'] (with a componentkey UUID) inside the
# LazyColumn under div[data-sdui-screen='com.linkedin.sdui.flagshipnav.mynetwork.CatchUpAll'], and
# the page carries NO data-view-name attributes and NO <li> elements around cards at all — which is
# why the pre-grounding chain below it matched zero cards on a feed showing ten. The old anchors
# stay as fallbacks in case LinkedIn re-ships that vocabulary. Non-person rows (ads, prompts) also
# render as listitems; the scraper's profile-link + classifier funnel filters them.
_CATCHUP_CARD_LOCATORS = [
    (By.CSS_SELECTOR, "div[data-sdui-screen*='CatchUp'] div[role='listitem']"),
    (By.CSS_SELECTOR, "main div[data-testid='lazy-column'] div[role='listitem']"),
    (By.CSS_SELECTOR, "div[data-view-name='catch-up-card']"),
    (By.CSS_SELECTOR, "li[data-view-name='catch-up-card']"),
    (By.CSS_SELECTOR, "section[data-view-name*='catch-up'] li"),
    (By.CSS_SELECTOR, "main li:has(a[href*='/in/'])"),
    (By.XPATH, "//main//li[.//a[contains(@href,'/in/')]]"),
]
_CATCHUP_PROFILE_LINK_LOCATORS = [
    (By.CSS_SELECTOR, "a[data-view-name='catch-up-card-profile-link']"),
    (By.CSS_SELECTOR, "a[href*='/in/']"),
]

# LinkedIn writes the congratulations for us — the catch-up card ships a pre-drafted response (a
# suggestion chip on the card, or the pre-filled compose box behind "Say congrats"). That draft is the
# BASELINE we send (owner review on PR #509): it is what the recipient expects from this surface, and
# it costs no LLM call. Our own AI is opt-in (catchup_message_source='ai').
_CATCHUP_SUGGESTED_TEXT_LOCATORS = [
    (By.CSS_SELECTOR, "button[data-view-name*='suggested-reply']"),
    (By.CSS_SELECTOR, "button[data-view-name*='message-suggestion']"),
    (By.CSS_SELECTOR, "[data-view-name*='catch-up-card-suggestion']"),
    (By.CSS_SELECTOR, "button[aria-label*='Congrats'], button[aria-label*='congrats']"),
]
# The card affordance that opens LinkedIn's pre-filled compose overlay. We only ever OPEN it to read
# the draft — never type, never submit (see _harvest_linkedin_draft).
_CATCHUP_MESSAGE_TRIGGER_LOCATORS = [
    (By.CSS_SELECTOR, "button[data-view-name*='catch-up-card-message']"),
    (By.CSS_SELECTOR, "button[aria-label*='Say congrats'], button[aria-label*='Send message']"),
    (By.CSS_SELECTOR, "button[aria-label*='Message']"),
]
# Only these read as "open the congratulations composer" — anything else on the card is left alone so
# a selector drift can't make us click Follow/Connect/Send.
_CATCHUP_TRIGGER_TEXT_RE = re.compile(r"say congrats|congrat|message", re.IGNORECASE)
_CATCHUP_COMPOSE_LOCATORS = [
    (By.CSS_SELECTOR, "div[role='dialog'] div[contenteditable='true']"),
    (By.CSS_SELECTOR, "div[role='dialog'] textarea"),
    (By.CSS_SELECTOR, "div[contenteditable='true'][aria-label*='message']"),
]
# The affordance labels and composer placeholders that live on the SAME nodes we read a draft off.
# `button[aria-label*='congrats']` matches LinkedIn's "Say congrats to Jane" trigger, and an empty
# composer renders its placeholder — both are long enough to clear the length floor, so without this
# the CHROME becomes the congratulations and we queue (or, on auto-approve, SEND) "Say congrats".
_CATCHUP_CHROME_RE = re.compile(
    r"(?:say\s+congrats(?:\s+to\s+\w+(?:\s+\w+){0,2})?"
    r"|congrats|congratulate(?:\s+\w+(?:\s+\w+){0,2})?"
    r"|send\s+(?:a\s+)?message(?:\s+to\s+\w+(?:\s+\w+){0,2})?"
    r"|write\s+(?:a\s+)?message"
    r"|message|reply|send|view\s+profile|see\s+more|more)[\s.…!]*",
    re.IGNORECASE)
_CATCHUP_DISMISS_LOCATORS = [
    (By.CSS_SELECTOR, "div[role='dialog'] button[aria-label*='Dismiss']"),
    (By.CSS_SELECTOR, "div[role='dialog'] button[aria-label*='Close']"),
]
# Opening LinkedIn's composer to read its draft is one extra interaction per QUALIFYING moment (never
# per card). Set to false to stay read-only on the card and use the per-milestone fallbacks below.
CATCHUP_HARVEST_LINKEDIN_DRAFT = os.getenv("CATCHUP_HARVEST_LINKEDIN_DRAFT", "true").lower() == "true"
# What LinkedIn's own suggestion sounds like, for when the feed doesn't surface one. Deliberately the
# same short, plain congratulations — NOT an AI rewrite, and never a pitch.
_CATCHUP_DEFAULT_CONGRATS = {
    "job_change": "Congrats on the new role, {first_name}!",
    "promotion": "Congrats on the promotion, {first_name}!",
    "work_anniversary": "Happy work anniversary, {first_name}!",
    "birthday": "Happy birthday, {first_name}!",
    "education": "Congrats on the milestone, {first_name}!",
    "in_the_news": "Great to see you in the news, {first_name}!",
}
CATCHUP_MESSAGE_MAX_CHARS = 300  # same budget as every other DM we send

# Base value of each milestone type. New job / promotion are the BD goldmine; a birthday is not.
# Read together with CATCHUP_MIN_SCORE (30): job change, promotion, in-the-news and work anniversary
# clear the bar on their own, while education and birthday only clear it for people who also match the
# user's targeting (+25 literal / +15 LLM topical). So enabling "birthday" means "congratulate the
# birthdays of people in my ICP", not "congratulate everyone's birthday".
_CATCHUP_EVENT_BASE = {
    "job_change": 50,
    "promotion": 50,
    "in_the_news": 35,
    "work_anniversary": 30,
    "education": 25,
    "birthday": 10,
}
_CATCHUP_ICP_BONUS = 25    # a targeting keyword/topic appears in the moment text
_CATCHUP_TOPIC_BONUS = 15  # LLM says the moment is topically relevant to the user's include_topics

# Ordered classifiers — first match wins, so more specific phrasings are tested first ("promoted to X
# at Y" and "celebrating 5 years at Y" both contain the new-position "at" pattern).
_CATCHUP_CLASSIFIERS = [
    ("promotion", re.compile(r"\bpromot(?:ed|ion)\b", re.IGNORECASE)),
    ("work_anniversary", re.compile(r"work anniversary|\b\d+\s*(?:year|yr)s?\b\s*(?:at|with)\b", re.IGNORECASE)),
    ("job_change", re.compile(r"start\w*\s+a\s+new\s+(?:position|job|role)|new\s+(?:position|role|job)\s+(?:as|at)\b"
                              r"|\bis\s+now\b.*\bat\b|\bjoined\b.*\bas\b", re.IGNORECASE)),
    ("birthday", re.compile(r"\bbirthday\b", re.IGNORECASE)),
    ("education", re.compile(r"\bgraduat\w*|\bearned\b.*\b(?:degree|diploma|certificat\w*)"
                             r"|\bcomplet\w*\b.*\b(?:degree|program|certificat\w*)", re.IGNORECASE)),
    ("in_the_news", re.compile(r"in the news|\bwas\s+(?:featured|mentioned|quoted)\b|\bfeatured in\b",
                               re.IGNORECASE)),
]

# Annual milestones dedup by YEAR; one-off milestones by MONTH (so a genuinely new job change later in
# the year can still earn a touch, but the same card re-appearing for days cannot).
_CATCHUP_ANNUAL_EVENTS = ("work_anniversary", "birthday")
# The milestone types worth nurturing toward a real BD conversation once they reply.
_CATCHUP_HIGH_VALUE_EVENTS = ("job_change", "promotion")
# How long after a high-value congratulations we check whether they replied, when the user configured
# no step-1 follow-up template of their own. Long enough that a same-day reply has landed.
CATCHUP_REPLY_CHECK_HOURS = int(os.getenv("CATCHUP_REPLY_CHECK_HOURS", "48"))


def _normalize_profile_url(url: str) -> str:
    """Strip query/fragment/trailing slash and percent-DECODE the path so the same person can't
    enter a dedup ledger twice under two URL spellings. Two spellings are real: LinkedIn appends
    tracking params to catch-up links, and SDUI escapes the hyphens of a vanity slug
    (`/in/jane%2Ddoe%2D1234` — issue #968's grounding run). Encoded and decoded are the SAME person,
    so the decoded form is the one key everything downstream compares on.
    """
    if not url:
        return ""
    parsed = urlparse(url.strip())
    path = unquote(parsed.path or "").rstrip("/")
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}{path}"
    return path


def _classify_catchup_moment(text: str) -> "str | None":
    """Map a catch-up card's text to a CatchupEventType value, or None when it isn't a moment we
    know how to congratulate (LinkedIn also renders suggestions/ads in this feed).
    """
    if not text:
        return None
    for event_type, pattern in _CATCHUP_CLASSIFIERS:
        if pattern.search(text):
            return event_type
    return None


def _catchup_event_period(event_type: str, now: datetime = None) -> str:
    """Dedup bucket for a milestone — see _CATCHUP_ANNUAL_EVENTS."""
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%Y") if event_type in _CATCHUP_ANNUAL_EVENTS else moment.strftime("%Y-%m")


def _catchup_excluded(moment: dict, prefs: dict) -> bool:
    """Honor the user's exclusion targeting — an excluded author/keyword is never touched at all."""
    haystack = f"{moment.get('name', '')} {moment.get('text', '')}".lower()
    for author in (prefs.get("exclude_authors") or []):
        if str(author).strip() and str(author).strip().lower() in (moment.get("name") or "").lower():
            return True
    for term in list(prefs.get("exclude_keywords") or []) + list(prefs.get("exclude_topics") or []):
        if str(term).strip() and str(term).strip().lower() in haystack:
            return True
    return False


def _catchup_contact_cooldown_active(user_id: int, profile_url: str, cooldown_days: int) -> bool:
    """True when this contact already received a catch-up within `cooldown_days` (issue #1078).

    A non-positive cooldown disables the guard. Read failures return False so a broken query never
    blocks the lane.
    """
    if cooldown_days <= 0:
        return False
    last_sent = last_catchup_sent_at(user_id, profile_url)
    if last_sent is None:
        return False
    return datetime.now(timezone.utc) - last_sent.replace(tzinfo=timezone.utc) < timedelta(
        days=cooldown_days)


def _catchup_contact_cap_reached(user_id: int, profile_url: str, max_touches: int,
                                 window_days: int = CATCHUP_CONTACT_CAP_WINDOW_DAYS) -> bool:
    """True when this contact has already received `max_touches` catch-ups in `window_days` (issue #1078).

    The window is the fixed rolling month, NOT the cooldown: a cap measured over the cooldown window
    can never be reached, because the cooldown blocks the second message long before the cap counts
    it — and a user who sets the cooldown to 0 would lose the cap with it. A non-positive
    `max_touches` disables the guard. Read failures return False.
    """
    if window_days <= 0 or max_touches <= 0:
        return False
    return count_catchup_touches_for_contact_in_window(user_id, profile_url, window_days) >= max_touches


def _score_catchup_moment(moment: dict, prefs: dict) -> int:
    """Score = milestone value + ICP fit. The literal keyword check is free; the LLM relevance check
    only runs when the literal one missed AND the user configured include_topics, so scoring a day's
    feed costs at most a handful of lem-simple calls.
    """
    score = _CATCHUP_EVENT_BASE.get(moment.get("event_type"), 0)
    haystack = f"{moment.get('name', '')} {moment.get('text', '')}".lower()
    literal_terms = (list(prefs.get("focus_topics") or []) + list(prefs.get("include_keywords") or [])
                     + list(prefs.get("include_topics") or []))
    if any(str(t).strip() and str(t).strip().lower() in haystack for t in literal_terms):
        return score + _CATCHUP_ICP_BONUS
    include_topics = prefs.get("include_topics") or []
    if include_topics and post_is_relevant(moment.get("text", ""), include_topics):
        score += _CATCHUP_TOPIC_BONUS
    return score


def _first_in_card(card: WebElement, locators: list) -> "WebElement | None":
    """First element inside a catch-up card matching the ordered locator chain. Searched directly
    (no WebDriverWait) because the card is already in the DOM — a per-card wait would cost the full
    timeout on every non-person card LinkedIn mixes into the feed.
    """
    for find_by, value in locators:
        try:
            els = card.find_elements(find_by, value)
        except (StaleElementReferenceException, NoSuchElementException):
            continue
        if els:
            return els[0]
    return None


def _card_profile_link(card: WebElement) -> "WebElement | None":
    return _first_in_card(card, _CATCHUP_PROFILE_LINK_LOCATORS)


def _clean_suggested_message(text: str) -> str:
    """Normalize a draft LinkedIn handed us: collapse whitespace, drop the button chrome, cap at the
    DM budget. Anything that doesn't look like a message (empty / a bare label) becomes ''.
    """
    cleaned = " ".join((text or "").replace("\n", " ").split()).strip()
    if len(cleaned) < 4 or _CATCHUP_CHROME_RE.fullmatch(cleaned):
        return ""
    return cleaned[:CATCHUP_MESSAGE_MAX_CHARS]


def _card_suggested_message(card: WebElement) -> str:
    """LinkedIn's suggested congratulations as rendered ON the card (a quick-reply chip), if any.
    Read-only — no clicking.
    """
    el = _first_in_card(card, _CATCHUP_SUGGESTED_TEXT_LOCATORS)
    if el is None:
        return ""
    try:
        return _clean_suggested_message(getText(el) or el.get_attribute("aria-label") or "")
    except (StaleElementReferenceException, NoSuchElementException):
        return ""


def _harvest_linkedin_draft(driver: WebDriver, card: WebElement, user_id: int = None) -> str:
    """Open LinkedIn's "Say congrats" composer for this card, read the message it pre-drafted, and
    close it WITHOUT sending. We never type into the box and never click a send/submit control, so the
    worst case is an opened-and-dismissed overlay. Best-effort: any miss returns '' and the caller
    falls back to the per-milestone default.
    """
    if not CATCHUP_HARVEST_LINKEDIN_DRAFT:
        return ""
    trigger = _first_in_card(card, _CATCHUP_MESSAGE_TRIGGER_LOCATORS)
    if trigger is None:
        return ""
    try:
        label = f"{getText(trigger) or ''} {trigger.get_attribute('aria-label') or ''}"
        if not _CATCHUP_TRIGGER_TEXT_RE.search(label):
            return ""  # a drifted selector matched something that isn't the congrats composer
        trigger.click()
        time.sleep(random.uniform(1, 2))
        compose = None
        for find_by, value in _CATCHUP_COMPOSE_LOCATORS:
            els = driver.find_elements(find_by, value)
            if els:
                compose = els[0]
                break
        draft = ""
        if compose is not None:
            draft = _clean_suggested_message(getText(compose) or compose.get_attribute("value") or "")
        return draft
    except (StaleElementReferenceException, NoSuchElementException, ElementNotInteractableException,
            WebDriverException) as e:
        log_warning("Could not read LinkedIn's pre-drafted catch-up message", exc=e, user_id=user_id,
                    action_type="catchup")
        return ""
    finally:
        _dismiss_catchup_composer(driver)


def _dismiss_catchup_composer(driver: WebDriver) -> None:
    """Close the composer overlay without sending — the dismiss control, else Escape."""
    try:
        for find_by, value in _CATCHUP_DISMISS_LOCATORS:
            els = driver.find_elements(find_by, value)
            if els:
                els[0].click()
                return
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    except (StaleElementReferenceException, NoSuchElementException, ElementNotInteractableException,
            WebDriverException) as e:
        # Nothing to recover: the overlay is already gone or unreachable, and the very next action is
        # a fresh driver.get() of the catch-up feed. Logged only, so a dismiss miss never fails a scan.
        log_warning("Could not dismiss the catch-up composer", exc=e, action_type="catchup")


def _scrape_catchup_moments(driver: WebDriver, max_moments: int = 40, user_id: int = None,
                            enabled_event_types: "set | None" = None) -> List[dict]:
    """Scrape the catch-up feed into [{name, profile_url, text, event_type, suggested_message}] — one
    entry per card, deduped by profile+text. Classification happens here so LinkedIn's pre-drafted
    message is only harvested for moments the user actually congratulates (`enabled_event_types`);
    pass None to skip harvesting entirely. Best-effort by design: a selector miss returns fewer moments
    (logged) rather than failing the run.
    """
    driver.get(CATCHUP_URL)
    time.sleep(random.uniform(3, 5))

    moments: List[dict] = []
    seen: set = set()
    cards_seen = 0
    for _ in range(3):  # the feed lazy-loads; a few scrolls cover a normal day's moments
        cards = find_all_first(driver, _CATCHUP_CARD_LOCATORS)
        cards_seen = max(cards_seen, len(cards))
        for card in cards:
            if len(moments) >= max_moments:
                break
            link = _card_profile_link(card)
            if link is None:
                continue
            try:
                profile_url = _normalize_profile_url(link.get_attribute("href") or "")
                text = normalize_public_text(getText(card) or "").replace("\n", " ").strip()
            except (StaleElementReferenceException, NoSuchElementException):
                continue
            if not profile_url or "/in/" not in profile_url or not text:
                continue
            key = (profile_url, text)
            if key in seen:
                continue
            seen.add(key)
            event_type = _classify_catchup_moment(text)
            suggested = ""
            if event_type and enabled_event_types and event_type in enabled_event_types:
                suggested = _card_suggested_message(card) or _harvest_linkedin_draft(
                    driver, card, user_id=user_id)
            moments.append({"name": _catchup_name_from_card(text, link), "profile_url": profile_url,
                            "text": text, "event_type": event_type, "suggested_message": suggested})
        if len(moments) >= max_moments:
            break
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(random.uniform(2, 4))

    if not cards_seen:
        # The #964 shape, caught this time: the chain matched no cards at all. Ask the page whether
        # it rendered any — `main div[role='listitem']` is independent of which SDUI screen wrapper
        # the chain leads with, so a rotated `data-sdui-screen` shows up here as drift instead of as
        # another quiet `no_moments` day. Warns ONLY on drift (see _report_zero_walk).
        _report_zero_walk(driver, _CATCHUP_CARD_CROSSCHECK_SEL, "Catch-up card walk",
                          user_id=user_id, action_type="catchup")
    elif not moments:
        # Cards rendered, none survived the funnel. DEBUG, not WARNING: an empty catch-up feed is a
        # normal day, ads and prompts render as listitems and are filtered by design, and the scan
        # beat runs daily per user — three warnings inside the escalation window would re-emit at
        # ERROR and file a grouped $exception for a no-op. The `no_moments` run status carries it.
        log_debug(f"Catch-up feed produced no moments from {cards_seen} card(s)", user_id=user_id,
                  action_type="catchup")
    return moments


# A catch-up card leads with the person's name and runs straight into the milestone on the SAME line
# ("Jay Bailey Completed 5 years at Emory University Congrats on your…"), so splitting on newlines
# keeps the whole card. These are the phrases the milestone half starts with — everything before the
# earliest one is the name.
_CATCHUP_NAME_STOP_RE = re.compile(
    r"\s+(?=(?:completed|celebrat\w*|congrats|congratulate|happy|started\s+a\s+new|is\s+now|"
    r"was\s+(?:promoted|featured|mentioned|quoted)|has\s+been|joined|graduat\w*|earned|"
    r"\d+\s*(?:year|yr)s?\b)\b)", re.IGNORECASE)
# A display name can carry a long credential tail ('DeWarren K. Langley, JD, MPA, MHFA, YMHFA, SWL'),
# so this only has to be tight enough to catch an uncut card, not to model a name.
_CATCHUP_NAME_MAX_WORDS = 12


def _catchup_name_from_card(text: str, link) -> str:
    """The card's person name, with the milestone half cut off.

    The profile link wraps the WHOLE card on the current surface, so its text is not a name — taking
    it verbatim put 'Jay Bailey Completed 5 years at Emory Un…' in the UI and in every downstream
    greeting (issue #1030). The name is what precedes the milestone phrase; when nothing looks like
    one, the profile slug is the fallback, because a wrong name is worse than a plain one.
    """
    try:
        raw = (getText(link) or "").strip().split("\n")[0]
    except (StaleElementReferenceException, NoSuchElementException):
        raw = ""
    if not raw:
        raw = (text or "").strip().split("\n")[0].split(" ·")[0]

    name = " ".join(_CATCHUP_NAME_STOP_RE.split(" ".join(raw.split()), maxsplit=1)[0].split())
    if not name or len(name.split()) > _CATCHUP_NAME_MAX_WORDS:
        try:
            derived = name_from_profile_url(link.get_attribute("href") or "")
        except (StaleElementReferenceException, NoSuchElementException, AttributeError):
            derived = ""
        name = derived.title() or name
    return name[:255]


def _draft_catchup_message(user_id: int, moment: dict, my_profile: LinkedInProfile,
                           source: str = "linkedin") -> "str | None":
    """The congratulations to send for this moment.

    'linkedin' (default): LinkedIn's OWN pre-drafted response — the suggestion it renders on the card
    or pre-fills in its composer — falling back to the matching plain one-liner when the feed gave us
    none. No LLM call. 'ai': the user's DM template refined to their voice (returns None when they
    deactivated the template for that event type).
    """
    first_name = (moment.get("name") or "").strip().split(" ")[0] or "there"
    if source != "ai":
        suggested = _clean_suggested_message(moment.get("suggested_message") or "")
        if suggested:
            return suggested
        default = _CATCHUP_DEFAULT_CONGRATS.get(moment["event_type"])
        return default.format(first_name=first_name) if default else None
    return build_dm_from_template(user_id, moment["event_type"], first_name, my_profile,
                                  event_detail=moment.get("event_detail") or moment.get("text", ""))


# The send drip beats every 20 minutes and the scan beat daily, so these outcomes are the STEADY
# state of a healthy account, not events. They log DEBUG (an expected no-op logged INFO is noise, and
# the throttle already logs its own reason in _skip_if_throttled) — the PostHog series is what
# carries them, and that's the series a "catch-up never sends" report has to be answered from.
_CATCHUP_QUIET_STATUSES = frozenset({CATCHUP_STATUS_NOTHING_TO_SEND, CATCHUP_STATUS_CAPPED,
                                     CATCHUP_STATUS_THROTTLED, CATCHUP_STATUS_DISABLED,
                                     CATCHUP_STATUS_DM_CAPPED, CATCHUP_STATUS_NOT_SENDABLE,
                                     CATCHUP_STATUS_AWAITING_APPROVAL, CATCHUP_STATUS_CONTACT_COOLDOWN,
                                     CATCHUP_STATUS_CONTACT_CAP, CATCHUP_STATUS_ALREADY_SENT})


def report_catchup_run(user_id: Optional[int], report: dict, task_name: str) -> None:
    """Ship ONE catch-up run report (issue #792) and log the same line locally. `user_id` is None for
    the fleet-wide beat dispatchers. Best-effort: a telemetry outage must never fail a scan that
    otherwise worked.
    """
    summary = ", ".join(f"{k}={report[k]}" for k in
                        ("moments", "classified", "enabled_type", "excluded", "duplicate",
                         "below_bar", "drafted", "dispatched", "capped", "inactive", "pending",
                         "requeued", "touch_id")
                        if k in report)
    status = report.get("status")
    emit = log_debug if status in _CATCHUP_QUIET_STATUSES else log_info
    emit(f"Catch-up {report.get('phase')} run: {status}" + (f" ({summary})" if summary else ""),
         user_id=user_id, task_name=task_name, action_type="catchup")
    try:
        track_catchup_run(user_id, report)
    except Exception as e:
        log_warning("Could not report the catch-up run", exc=e, user_id=user_id,
                    task_name=task_name, action_type="catchup")


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  reject_on_worker_lost=True, rate_limit='2/m', queue='se_outreach')
def automate_catchup_touches(self, user_id: int, max_moments: int = 40, max_drafts: int = 10):
    """Scrape -> score -> draft the user's LinkedIn Catch-up congratulations (issue #482).

    NOTHING is sent here. Qualifying moments become 'pending' rows in catchup_touches for human
    approval (or 'approved' when the user opted into catchup_touch_mode='auto_approve'), and the
    capped scanner sends them later. Only milestone types the user enabled are drafted, each
    milestone is deduped on (person, event_type, period), moments scoring below CATCHUP_MIN_SCORE
    are tombstoned as 'skipped' so we neither draft nor re-score them, and the per-contact frequency
    guard (issue #1078) holds back any new event for a contact that already received a catch-up in
    the configured window. The message itself is LinkedIn's own pre-drafted response unless the user
    opted into catchup_message_source='ai'.

    EVERY run reports (issue #792) — including the ones that draft nothing — with the per-stage
    funnel counts, because a quiet lane and a broken one are otherwise the same silence.
    """
    task_name = "automate_catchup_touches"
    prefs = get_engagement_preferences(user_id)
    enabled = {str(t) for t in (prefs.get("catchup_event_types") or [])}
    auto_approve = str(prefs.get("catchup_touch_mode") or "pre_review") == "auto_approve"
    source = str(prefs.get("catchup_message_source") or "linkedin")
    report = {"phase": CATCHUP_PHASE_SCAN, "auto_approve": auto_approve, "message_source": source}

    if not enabled:
        report["status"] = CATCHUP_STATUS_DISABLED
        report_catchup_run(user_id, report, task_name)
        return f"Catch-up touches disabled for user {user_id} (no event types enabled)"

    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id,
                                                                   session_name="Catch-up Moments")
    except LinkedInRateLimited as e:
        log_info(f"automate_catchup_touches deferred (throttled): {e}")
        report["status"] = CATCHUP_STATUS_THROTTLED
        report_catchup_run(user_id, report, task_name)
        return "Catch-up scan deferred (LinkedIn throttled)"
    except Exception as e:
        log_error("Failed to get profile for catch-up scan", exc=e, user_id=user_id,
                  task_name=task_name)
        report["status"] = CATCHUP_STATUS_SESSION_FAILED
        report_catchup_run(user_id, report, task_name)
        return f"Failed to start catch-up scan: {e}"

    drafted = skipped = 0
    try:
        moments = _scrape_catchup_moments(driver, max_moments=max_moments, user_id=user_id,
                                          enabled_event_types=enabled)
    except LinkedInRateLimited as e:
        log_info(f"automate_catchup_touches deferred mid-scrape (throttled): {e}")
        report["status"] = CATCHUP_STATUS_THROTTLED
        report_catchup_run(user_id, report, task_name)
        return "Catch-up scan deferred (LinkedIn throttled)"
    except Exception as e:
        log_error("Catch-up scrape failed", exc=e, user_id=user_id, task_name=task_name)
        report["status"] = CATCHUP_STATUS_SCRAPE_FAILED
        report_catchup_run(user_id, report, task_name)
        return f"Catch-up scrape failed: {e}"
    finally:
        quit_gracefully(driver)

    cooldown_days = int(prefs.get("min_catchup_contact_interval_days") or 0)
    max_per_contact = int(prefs.get("max_catchup_touches_per_contact_days") or 0)
    funnel = {"classified": 0, "enabled_type": 0, "excluded": 0, "duplicate": 0, "below_bar": 0,
              "contact_cooldown": 0, "contact_cap": 0}
    for moment in moments:
        if drafted >= max_drafts:
            break
        event_type = moment.get("event_type") or _classify_catchup_moment(moment.get("text", ""))
        if event_type is None:
            continue
        funnel["classified"] += 1
        if event_type not in enabled:
            continue
        funnel["enabled_type"] += 1
        if _catchup_excluded(moment, prefs):
            funnel["excluded"] += 1
            continue
        moment["event_type"] = event_type
        period = _catchup_event_period(event_type)
        if has_catchup_touch(user_id, moment["profile_url"], event_type, period):
            funnel["duplicate"] += 1
            continue
        # Per-contact frequency guard (issue #1078): don't stack congratulations to the same person
        # across different events within the configured window.
        if _catchup_contact_cooldown_active(user_id, moment["profile_url"], cooldown_days):
            funnel["contact_cooldown"] += 1
            continue
        if _catchup_contact_cap_reached(user_id, moment["profile_url"], max_per_contact):
            funnel["contact_cap"] += 1
            continue
        score = _score_catchup_moment(moment, prefs)
        if score < CATCHUP_MIN_SCORE:
            insert_catchup_touch(user_id, moment["profile_url"], event_type, period,
                                 person_name=moment.get("name"), event_detail=moment.get("text"),
                                 score=score, status=CatchupTouchStatus.SKIPPED)
            funnel["below_bar"] += 1
            skipped += 1
            continue
        try:
            message = _draft_catchup_message(user_id, moment, my_profile, source=source)
        except Exception as e:
            log_warning("Could not draft catch-up message", exc=e, user_id=user_id, action_type="catchup")
            continue
        if not message:
            continue
        status = CatchupTouchStatus.APPROVED if auto_approve else CatchupTouchStatus.PENDING
        if insert_catchup_touch(user_id, moment["profile_url"], event_type, period,
                                person_name=moment.get("name"), event_detail=moment.get("text"),
                                message=message, score=score, status=status):
            drafted += 1

    report.update(funnel, moments=len(moments), drafted=drafted)
    if drafted:
        report["status"] = CATCHUP_STATUS_DRAFTED
    else:
        report["status"] = CATCHUP_STATUS_NO_MOMENTS if not moments else CATCHUP_STATUS_NONE_QUALIFIED
    report_catchup_run(user_id, report, task_name)
    return (f"Catch-up: {len(moments)} moment(s) scanned, {drafted} drafted "
            f"({'approved' if auto_approve else 'awaiting approval'}), {skipped} below the bar")


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'keys': ['touch_id']},
                  reject_on_worker_lost=True, rate_limit='1/m', queue='se_outreach')
def send_catchup_touch(self, touch_id: int):
    """Send one APPROVED catch-up congratulations (issue #482). Enforces BOTH the catch-up per-day cap
    and the overall DM cap at send time — either one trips the touch back to 'approved' for the next
    scan rather than sending — and reuses send_dm_now, so it honors the 429 breaker / kill-switch and
    the shared DM logging. A high-value milestone also enqueues the user's follow-up sequence, which is
    what routes a replying prospect into the BD nurture.

    Issue #1078 adds two extra guards: a durable `catchup_send_attempts` claim written BEFORE the DM
    goes out (so retries / worker restarts can never double-send the same milestone), and a per-contact
    cooldown/cap that defers a touch when the same person already received a catch-up within the window.

    EVERY outcome reports (issue #792). A deferral puts the touch back to 'approved' and the drip
    re-dispatches it 20 minutes later, so without this the send phase's `dispatched` count climbs all
    day on a lane that has never delivered a single message — which IS the reported symptom.
    """
    task_name = "send_catchup_touch"

    def _report(status: str, user_id: Optional[int]) -> None:
        report_catchup_run(user_id, {"phase": CATCHUP_PHASE_DELIVER, "status": status,
                                     "touch_id": touch_id}, task_name)

    touch = get_catchup_touch(touch_id)
    if not touch or touch["status"] not in (CatchupTouchStatus.APPROVED, CatchupTouchStatus.SENDING):
        _report(CATCHUP_STATUS_NOT_SENDABLE, touch.get("user_id") if touch else None)
        return f"Catch-up touch {touch_id} not sendable (status={touch['status'] if touch else 'missing'})"
    if not (touch.get("message") or "").strip():
        update_catchup_touch_status(touch_id, CatchupTouchStatus.SKIPPED)
        log_warning("Catch-up touch approved with no message; skipping", user_id=touch["user_id"],
                    action_type="catchup")
        _report(CATCHUP_STATUS_NO_MESSAGE, touch["user_id"])
        return f"Catch-up touch {touch_id} skipped (no message)"

    user_id = touch["user_id"]
    prefs = get_engagement_preferences(user_id)
    # The saved cap can only go as high as the user's plan allows (10/day premium, 5/day otherwise) —
    # re-checked here so a downgrade takes effect immediately, not at the next settings save.
    daily_cap = min(int(prefs.get("max_catchup_touches_per_day") or 0),
                    max_catchup_touches_allowed(user_id))
    if count_catchup_touches_sent_today(user_id) >= daily_cap:
        update_catchup_touch_status(touch_id, CatchupTouchStatus.APPROVED)  # retry on the next scan
        _report(CATCHUP_STATUS_CAPPED, user_id)
        return f"Catch-up touch {touch_id} deferred (daily catch-up cap reached)"
    if count_dms_sent_today(user_id) >= int(prefs.get("max_dms_per_day") or 0):
        update_catchup_touch_status(touch_id, CatchupTouchStatus.APPROVED)
        _report(CATCHUP_STATUS_DM_CAPPED, user_id)
        return f"Catch-up touch {touch_id} deferred (daily DM cap reached)"

    cooldown_days = int(prefs.get("min_catchup_contact_interval_days") or 0)
    max_per_contact = int(prefs.get("max_catchup_touches_per_contact_days") or 0)
    if _catchup_contact_cooldown_active(user_id, touch["profile_url"], cooldown_days):
        update_catchup_touch_status(touch_id, CatchupTouchStatus.APPROVED)
        _report(CATCHUP_STATUS_CONTACT_COOLDOWN, user_id)
        return f"Catch-up touch {touch_id} deferred (per-contact cooldown)"
    if _catchup_contact_cap_reached(user_id, touch["profile_url"], max_per_contact):
        update_catchup_touch_status(touch_id, CatchupTouchStatus.APPROVED)
        _report(CATCHUP_STATUS_CONTACT_CAP, user_id)
        return f"Catch-up touch {touch_id} deferred (per-contact cap)"

    # Durable send claim: only one worker can insert this milestone identity. If another already
    # did, the row is already sent (or sending) and we must not call LinkedIn again.
    event_period = touch.get("event_period") or _catchup_event_period(touch["event_type"])
    if not claim_catchup_send_attempt(touch_id, user_id, touch["profile_url"], touch["event_type"],
                                      event_period):
        # A claim we did not write, on a row that never reached `sent`, means a send was lost between
        # the claim and its status update — rare enough to be worth a defect, and never routine.
        log_warning(f"Catch-up touch {touch_id} already has a send claim; skipping",
                    user_id=user_id, action_type="catchup")
        # Leave the row as SENT if the claim exists; a missing status update is the only reason we'd
        # arrive here with the claim already present.
        if touch["status"] != CatchupTouchStatus.SENT:
            update_catchup_touch_status(touch_id, CatchupTouchStatus.SENT)
        _report(CATCHUP_STATUS_ALREADY_SENT, user_id)
        return f"Catch-up touch {touch_id} already sent"

    try:
        sent = send_dm_now(user_id, touch["profile_url"], touch["message"],
                           person_name=touch.get("person_name"))
    except LinkedInRateLimited as e:
        log_info(f"send_catchup_touch: throttled, deferring {touch_id}: {e}")
        # The breaker refused before a composer was ever opened, so nothing was sent — give the claim
        # back, or the deferral we are about to write could never be retried: the next attempt would
        # lose the claim and mark this touch `sent` having sent nothing.
        release_catchup_send_attempt(user_id, touch["profile_url"], touch["event_type"], event_period)
        update_catchup_touch_status(touch_id, CatchupTouchStatus.APPROVED)
        _report(CATCHUP_STATUS_THROTTLED, user_id)
        return f"Catch-up touch {touch_id} deferred (LinkedIn throttled)"
    update_catchup_touch_status(touch_id, CatchupTouchStatus.SENT if sent else CatchupTouchStatus.FAILED)
    _report(CATCHUP_STATUS_SENT if sent else CATCHUP_STATUS_FAILED, user_id)
    if sent:
        first_name = (touch.get("person_name") or "").strip().split(" ")[0] or "there"
        _schedule_catchup_followup(user_id, touch["profile_url"], first_name, str(touch["event_type"]))
    return f"Catch-up touch {touch_id} -> {'sent' if sent else 'failed'}"


def _schedule_catchup_followup(user_id: int, profile_url: str, first_name: str, event_type: str) -> None:
    """Queue the dm_followups row that process_user_followups works off after a congratulations goes out.

    When the user configured a step-1 template this is the normal follow-up sequence. When they didn't
    — the out-of-the-box case, since the catch-up defaults only cover step 0 — a high-value milestone
    STILL needs the row, because the reply check it drives is what routes a replying prospect into the
    outreach funnel (#482, step 5). With no template build_dm_from_template returns None, so that row
    only ever checks for a reply and is then stopped; it can never send an extra DM.
    """
    try:
        if get_dm_template(user_id, event_type, 1):
            enqueue_next_followup(user_id, profile_url, first_name, event_type, 0)
            return
        if event_type not in _CATCHUP_HIGH_VALUE_EVENTS:
            return
        due = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=CATCHUP_REPLY_CHECK_HOURS)
        enqueue_followup(user_id, profile_url, first_name, event_type, 1, due)
    except Exception as e:
        log_warning("Could not schedule the catch-up reply check", exc=e, user_id=user_id,
                    action_type="catchup")


def _route_replied_catchup_to_funnel(user_id: int, followup: dict) -> None:
    """A reply to a new-job/promotion congratulations is the opening of a real conversation — drop that
    prospect into the comment-first outreach funnel at the DM stage as 'pending' so the operator can
    nurture it toward a BD conversation (issue #482, step 5). Approval-gated like every funnel stage,
    and never duplicates a target already in the funnel. Best-effort: never breaks the follow-up loop.
    """
    event_type = str(followup.get("event_type") or "")
    if event_type not in _CATCHUP_HIGH_VALUE_EVENTS:
        return
    profile_url = followup.get("profile_url")
    try:
        if get_outreach_target_by_url(user_id, profile_url):
            return
        target = {"target_name": followup.get("first_name"), "target_profile_url": profile_url}
        insert_outreach_target(user_id, profile_url, target_name=followup.get("first_name"),
                               draft_text=_draft_funnel_stage(user_id, OutreachStage.DM, target),
                               stage=OutreachStage.DM, status=OutreachStatus.PENDING)
        log_info("Routed replying catch-up prospect into the outreach funnel", user_id=user_id,
                 action_type="catchup")
    except Exception as e:
        log_warning("Could not route catch-up reply into the outreach funnel", exc=e,
                    user_id=user_id, action_type="catchup")


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True}, reject_on_worker_lost=True,
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


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'keys': ['post_id']}, reject_on_worker_lost=True,
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
