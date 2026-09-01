"""Outreach: every DM LEM sends as the user, and every walk that decides who to send one to.

Step 5 of the `run_automation.py` split (#1154), and the last cluster. DMs and outreach move as ONE
module because they are one graph, not two: the appreciation walk, the profile-viewer walk, the
connect-candidate scan, the outreach funnel and the catch-up lane all end at the SAME send
(`send_dm_now` / `send_private_dm`) and the same follow-up ladder (`enqueue_next_followup` /
`process_user_followups`), and every one of them reads the same DM-templating and reply-detection
helpers. Measured before anything moved: 12 tasks, 161 symbols in the closure, and **zero** symbols
shared with anything left behind — nothing had to stay.

**Every task here pins `name='cqc_lem.app.run_automation.<fn>'`, and that is load-bearing.** Celery
derives a task's name from `<module>.<function>`, so moving one RENAMES it silently: eight of these
twelve are named as plain strings in `celeryconfig.task_routes` and would stop matching, messages
already queued under the old name would be rejected `NotRegistered` and dropped, and the `QueueOnce`
lock key embeds the task name, so it would re-key mid-deploy — for `send_catchup_touch`, whose lock
is keyed on `touch_id`, that means DMing the same person twice for the same milestone.
`scripts/restructure/celery_inventory.py` diffed across the move is what proves none of that happened.

Two tasks re-queue THEMSELVES with `globals()[current_function_name].apply_async`, where
`current_function_name` is `frame.f_code.co_name`: `automate_appreciation_dms_for_user` (through
`_dispatch_appreciation_dms`) and `automate_profile_viewer_engagement`. The lookup reads THIS
module's globals and stays correct — but only because each task and its module moved together.
Never split, wrap or rename one.

The module imports NOTHING from `run_automation` — that is what keeps the dependency one-way, since
`run_automation` imports the twelve tasks back (plus `report_catchup_run` and the catch-up phase /
status vocabulary) so `run_scheduler` and `api/*` keep reading them from there by name. The two
edges that run into other clusters are WIRE edges, not code edges: `generate_and_post_comment` and
`_fire_funnel_stage` dispatch `comment_on_post` into `app.engagement.feed`, and
`engage_with_profile_viewer` / `_fire_funnel_stage` dispatch `invite_to_connect` into
`app.engagement.invites`. Neither of those modules imports anything back.

Posture for every lane below — appreciation sources and the durable claim (#968), the message-thread
ladder and what "sent" means (#731/#1030), DM auto-nurture (#616), the outreach funnel, the connect
candidate scan and the catch-up lane (#482) — is `docs/engagement-automation.md`.

Two rules cut across everything in this file.

**Pacing is not a safety gate.** `utilities/human_pacing.py` (issue #626) decides only how slowly we
act and fails OPEN; the hard stops are separate and checked in their own right — the 429 breaker in
`linkedin/rate_limit.py` and the suppression tripwire's engagement pause (#629).

**Success is the OUTCOME being present, never a click having landed** (#1013, `docs/sdui-selenium-notes.md`).
A DM is sent only once it is visible in the thread. The cost of getting this wrong is not a bad
metric — it is the account.
"""

import inspect
import json
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import parse_qs, unquote, urlparse

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

# Two WIRE edges out of this cluster — a name on a message, never a call. `generate_and_post_comment`
# and `_fire_funnel_stage` hand a drafted comment to the feed cluster's `comment_on_post`, and
# `engage_with_profile_viewer` / `_fire_funnel_stage` hand an invite to the connect rail. Neither
# `app.engagement.feed` nor `app.engagement.invites` imports anything back, so the edge runs one way.
from cqc_lem.app.engagement.feed import comment_on_post
from cqc_lem.app.engagement.invites import invite_to_connect
from cqc_lem.app.my_celery import app as shared_task
from cqc_lem.app.queue_once import QueueOnce
from cqc_lem.utilities.ai.ai_helper import (
    ai_check_message_history,
    generate_ai_response,
    generate_nurture_dm,
    get_ai_message_refinement,
    get_or_create_profile_synthesis,
    lint_repaired,
    post_is_relevant,
    summarize_recent_activity,
)
from cqc_lem.utilities.ai.content_alignment import (
    humanize_text,
)
from cqc_lem.utilities.ai.dm_nurture import (
    classify_reply_intent,
    is_stop_intent,
    nurture_delay_hours,
    recipient_context,
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
    CONNECTION_REQUEST_SOURCE_PROFILE_VIEWER,
    SCHEDULED_DM_SOURCE_ARTIFACT,
    SCHEDULED_DM_SOURCE_NURTURE,
    SCHEDULED_DM_SOURCE_PROFILE_VIEWER,
    CatchupTouchStatus,
    ConnectionRequestStatus,
    FollowupStatus,
    LeadSignalChannel,
    LeadSignalSource,
    LeadSignalStatus,
    LogActionType,
    LogResultType,
    OutreachStage,
    OutreachStatus,
    ScheduledDmStatus,
    claim_appreciation_touch,
    claim_catchup_send_attempt,
    count_catchup_touches_for_contact_in_window,
    count_catchup_touches_sent_today,
    count_comments_today,
    count_dms_sent_today,
    count_invites_sent_today,
    count_open_connection_requests,
    count_open_outreach_targets,
    count_scheduled_dms_created_today,
    enqueue_followup,
    get_approved_outreach_targets,
    get_catchup_touch,
    get_dm_history_for_profile,
    get_dm_template,
    get_due_followups,
    get_engagement_preferences,
    get_engagement_targets,
    get_engager_candidates,
    get_lead_signal,
    get_linkedin_profile_url_by_user_id,
    get_outreach_target_by_url,
    get_profile_facts,
    get_recent_comment_texts,
    get_requested_person_keys,
    get_user_blog_url,
    get_user_id,
    get_user_password_pair_by_id,
    has_appreciation_touch,
    has_catchup_touch,
    has_engaged_url_with_x_days,
    has_open_scheduled_dm,
    has_user_commented_on_post_url,
    insert_catchup_touch,
    insert_connection_request,
    insert_new_log,
    insert_outreach_target,
    insert_scheduled_dm,
    last_catchup_sent_at,
    mark_followup,
    max_catchup_touches_allowed,
    record_unreadable_read,
    release_catchup_send_attempt,
    reset_unreadable_reads,
    stop_followups_for_profile,
    update_catchup_touch_status,
    update_lead_signal,
    update_outreach_target,
    update_outreach_target_status,
)
from cqc_lem.utilities.dm_templates import _draft_connect_note, render_dm_placeholders
from cqc_lem.utilities.human_pacing import (
    ACTION_COMMENT,
    ACTION_DM,
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
from cqc_lem.utilities.linkedin.composer import (
    _COMMENTLIST_TEXTBOX,
    _comment_container,
    _comment_header_author,
    _comment_items,
    _comment_items_from_thread,
    _reply_under_comment_inline,
    comment_author_identity,
)
from cqc_lem.utilities.linkedin.helper import (
    clean_person_name,
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
from cqc_lem.utilities.linkedin.profile import LinkedInProfile
from cqc_lem.utilities.linkedin.rate_limit import (
    LinkedInRateLimited,
)
from cqc_lem.utilities.linkedin.session import get_current_profile
from cqc_lem.utilities.linkedin_formatter import normalize_public_text
from cqc_lem.utilities.log_escalation import masked_recipient
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning
from cqc_lem.utilities.observability import (
    FEATURE_COMMENT,
    FEATURE_DM,
    attribute_llm_cost,
    llm_attribution,
    track_catchup_run,
)
from cqc_lem.utilities.selenium_util import (
    click_element_wait_retry,
    click_first,
    find_all_first,
    get_driver_wait_pair,
    get_element_wait_retry,
    getText,
    is_tab_crashed,
    quit_gracefully,
    wait_for_ajax,
)

__all__ = [
    # The twelve tasks, plus the two non-task names other modules read from `run_automation`:
    # `report_catchup_run` (the beat's own reporter) and the catch-up phase/status vocabulary
    # `run_scheduler` labels its reports with. `run_automation` re-exports every one of these,
    # because `run_scheduler` and `api/*` still import them from there.
    "automate_appreciation_dms_for_user",
    "automate_catchup_touches",
    "automate_profile_viewer_engagement",
    "engage_with_profile_viewer",
    "process_outreach_funnel",
    "process_user_followups",
    "report_catchup_run",
    "scan_connection_candidates",
    "scan_outreach_funnel_targets",
    "send_catchup_touch",
    "send_lead_response",
    "send_private_dm",
    "send_scheduled_dm",
    # The catch-up vocabulary. `run_scheduler` reads eight of these to label its scan/send reports;
    # `zero_walk_verdict` is the catch-up walk's alias of the ONE grader. Nothing INSIDE this module
    # reads the four below, so CodeQL reports them as unused globals unless the public surface is
    # declared (a `# lgtm[...]` comment is the retired syntax and no longer counts).
    "CATCHUP_PHASE_SEND",
    "CATCHUP_STATUS_DISPATCHED",
    "CATCHUP_STATUS_INACTIVE",
    "zero_walk_verdict",
]


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
# company_page_inviter need it too and both are imported BY this module. Aliased under the names
# this module already used so every call site (and its tests) keeps one spelling.
# `app/engagement/feed.py` and `app/engagement/posting.py` alias the same upstream originals rather
# than importing these, which is what lets none of the three import another (#1154).
_CATCHUP_CARD_CROSSCHECK_SEL = "main div[role='listitem']"

zero_walk_verdict = _zw.zero_walk_verdict
_report_zero_walk = _zw.report_zero_walk
_grade_zero_walk = _zw.grade_zero_walk


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


def _mentions_page_native_count(driver, user_id: int = None) -> "int | None":
    """How many mention sentences the mentions page renders ITSELF, counted without the card chain.

    Every rung of `_MENTION_CARD_LOCATORS` is about the card CONTAINER, so a single SDUI wrapper
    rename answers zero to all four and reads exactly like a month with no mentions (#1374). The
    page's own text depends on none of them, and it is the same sentence production requires of a
    card, so a non-zero count here is cards the walk should have seen.

    None when the text could not be read at all: "we could not ask the page" must never be recorded
    as "the page said zero" (see `utilities/linkedin/zero_walk.py`).
    """
    root = None
    for tag in ("main", "body"):
        try:
            root = driver.find_element(By.TAG_NAME, tag)
            break
        except WebDriverException:
            continue
    if root is None:
        log_debug("Mentions page rendered neither <main> nor <body>", user_id=user_id,
                  action_type="scrape")
        return None
    try:
        text = getText(root)
    except WebDriverException as e:
        log_debug(f"Could not read the mentions page text: {e}", user_id=user_id,
                  action_type="scrape")
        return None
    return len(_MENTION_TEXT_RE.findall(text)) if isinstance(text, str) else None


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
        # #1374: zero cards is not "no mentions" until the page agrees. Grade it against the page's
        # own sentences — evidence the four card locators do not depend on — so a rotated wrapper
        # reads as drift (WARNING) instead of another quiet day, and a genuinely empty feed still
        # logs DEBUG. Unreadable grounds nothing either way.
        _grade_zero_walk(_mentions_page_native_count(driver, user_id), "Mention card walk",
                         user_id=user_id, action_type="scrape", url=_MENTIONS_URL)
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
            if opened.events:
                # Message events are on screen but their sender is unreadable — a real read failure
                # (the sender selector rotated). Warn (and escalate) so it gets looked at.
                log_warning(f"Reply-detection: thread opened via '{opened.route}' with message "
                            f"events but no sender could be read — treating as UNKNOWN, not as "
                            f"'no reply'", user_id=user_id, action_type="followup")
            else:
                # Zero events on an OPEN thread means a bare compose overlay: `open_message_thread`
                # only reports opened when it sees events OR a composer, so no events implies the
                # composer. There is nothing to read a sender from, and returning UNKNOWN to skip is
                # the correct #731 behaviour — an expected no-op, so DEBUG, not a warning that would
                # escalate working behaviour into a $exception. Trade-off: `events` and the sender
                # read share the event-node selector, so a rotation of THAT selector also reads as
                # zero events and lands here at DEBUG; no per-check signal separates the two, and a
                # global rotation surfaces as the follow-up lane returning no REPLIED verdicts.
                log_debug(f"Reply-detection: thread opened via '{opened.route}' as a bare composer "
                          f"with no messages — nothing to read, treating as UNKNOWN",
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
# Issue #1815: a thread that never becomes readable used to stay due forever, so every */30min
# `send-due-dm-followups` beat re-opened it — 48 Chrome sessions a day for a read that never
# succeeds. A few UNKNOWN reads on the ordinary cadence are left alone (a rotated selector usually
# clears on the very next run); only once that stops looking transient does the row back off, by
# growing intervals, capped, and it stays 'pending' the whole time — #731's UNKNOWN-never-sends is
# a read-frequency question here, not a send-safety one.
UNREADABLE_READ_CEILING = 3            # reads at/under this: unchanged cadence, no due_at push
UNREADABLE_READ_BACKOFF_HOURS = 2      # first backoff step once the ceiling is crossed
UNREADABLE_READ_BACKOFF_CAP_HOURS = 48  # a single backoff step never exceeds this


def _unreadable_backoff_hours(unreadable_reads: int) -> int:
    """How many hours to defer the next read of a thread that has gone UNKNOWN this many times.

    0 means "do not defer" — at or under `UNREADABLE_READ_CEILING` the row keeps the ordinary
    cadence, because a rotated selector usually clears on the very next run and deferring a
    transient miss would delay a real follow-up for nothing.

    Past the ceiling it doubles from `UNREADABLE_READ_BACKOFF_HOURS` (2h, 4h, 8h, …) and stops at
    `UNREADABLE_READ_BACKOFF_CAP_HOURS`. The EXPONENT is bounded, not just the result: a row that
    has been dead for two years otherwise builds a several-hundred-digit integer for `min()` to
    throw away.

    Args:
        unreadable_reads: Consecutive UNKNOWN reads INCLUDING the one being recorded now.

    Returns:
        Hours to push `due_at` out by, or 0 to leave it alone.
    """
    steps = unreadable_reads - UNREADABLE_READ_CEILING
    if steps <= 0:
        return 0
    # Once 2 ** n clears the cap the answer is the cap forever, so the exponent never needs to grow
    # past that — +1 keeps the first capped step exact rather than off by one doubling.
    max_exponent = (UNREADABLE_READ_BACKOFF_CAP_HOURS // UNREADABLE_READ_BACKOFF_HOURS).bit_length()
    return min(UNREADABLE_READ_BACKOFF_CAP_HOURS,
               UNREADABLE_READ_BACKOFF_HOURS * (2 ** min(steps - 1, max_exponent)))


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
            log_warning(f"DM nurture: a reply from {masked_recipient(first_name, profile_url)} was "
                        f"detected but its text could not be read — nothing to draft against",
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
            # DEBUG: "one open draft per conversation" is the designed rule the comment above
            # states, and a thread is re-checked on a schedule — an expected no-op.
            log_debug(f"DM nurture: {first_name or profile_url} already has a queued draft; skipping",
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
        # Who they are, from stored data only — no profile visit is opened to write a draft (#1625).
        # The follow-up row's own event_type is what says why this thread exists; on a 'nurture' row
        # that IS the sequence, so it contributes no origin line rather than inventing one.
        who = recipient_context(profile_url=profile_url, first_name=first_name,
                                event_type=followup.get("event_type"), user_id=user_id)
        message = None
        try:
            message = generate_nurture_dm(
                their_message, intent, my_profile, first_name=first_name,
                template_hint=(tmpl or {}).get("template_text"),
                history=get_dm_history_for_profile(user_id, profile_url),
                prefs=prefs, profile_synthesis=profile_synthesis, recipient_context=who)
        except Exception as e:
            log_warning("Nurture draft failed; falling back to the template", exc=e,
                        user_id=user_id, action_type="dm")
        if not message:
            message = build_dm_from_template(user_id, NURTURE_EVENT_TYPE, first_name, my_profile, step=step)
            if message:
                # The template answers nobody's reply — it is the least relevant thing this queue can
                # hold, so how often it fires is worth reading. INFO, not WARNING: it is a designed
                # fallback, and warning on it would file a defect every time the slop lint trims a draft.
                log_info(f"DM nurture: fell back to the '{NURTURE_EVENT_TYPE}' template for "
                         f"{first_name or profile_url} (step {step}) — that draft does not answer "
                         f"their reply", user_id=user_id, action_type="dm")
        if not message:
            log_warning(f"DM nurture: no draft could be produced for "
                        f"{masked_recipient(first_name, profile_url)} (step {step}) — the LLM returned "
                        f"nothing and no 'nurture' template exists for that step",
                        user_id=user_id, action_type="dm")
            return None

        due = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=nurture_delay_hours(intent))
        status = ScheduledDmStatus.APPROVED if _nurture_auto_approve() else ScheduledDmStatus.PENDING
        dm_id = insert_scheduled_dm(user_id, profile_url, message, due,
                                    recipient_name=first_name or None, status=status,
                                    source=SCHEDULED_DM_SOURCE_NURTURE)
        if not dm_id:
            log_warning(f"DM nurture: drafted a next message for "
                        f"{masked_recipient(first_name, profile_url)} but the scheduled_dms insert "
                        f"failed", user_id=user_id, action_type="dm")
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


@shared_task.task(name='cqc_lem.app.run_automation.process_user_followups',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_outreach')
def process_user_followups(self, user_id: int, max_per_run: int = 20):
    """Send this user's due DM follow-ups: anyone who has replied gets their sequence stopped and,
    instead of going cold, an approval-gated context-aware next message (issue #485); everyone else
    gets the next-step template rendered in the user's voice, sent, marked, and re-scheduled.
    """
    # Aware UTC: `get_due_followups` normalizes through `to_naive_utc`, the same conversion every
    # writer of `dm_followups.due_at` applies, so the comparison cannot straddle two clocks
    # (docs/timezone-contract.md).
    due = [f for f in get_due_followups(datetime.now(timezone.utc))
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
        # needs_images=True (#1774): this session's own `check_dm_replied` walks
        # `open_message_thread`'s 6-route ladder, which reads `/messaging/*` — blocked images stop
        # that surface's fastboot app from ever mounting.
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Follow-ups",
                                                                    needs_images=True)
    except Exception as e:
        if is_tab_crashed(e):
            # get_current_profile already logs this at WARNING where it's detected (the first
            # login navigation) before re-raising — warning again here would double-file the SAME
            # tab-crash occurrence under a second message/call-site, defeating the point of
            # downgrading it (issue #1749). This catch is a wrapper re-reporting a reason already
            # logged where it happened, same as the invite_to_connect precedent in
            # utilities/CLAUDE.md — DEBUG, not another WARNING.
            log_debug("Browser tab crashed while starting follow-ups", exc=e, user_id=user_id,
                      task_name="process_user_followups")
        else:
            log_error("Error getting profile for follow-ups", exc=e, user_id=user_id,
                      task_name="process_user_followups")
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
                # anyway is the one irreversible mistake here (issue #731) — leave the row 'pending'
                # so the next run re-reads it, and let the miss be greppable.
                skipped += 1
                # DEBUG, not a warning: check_dm_replied (and the open_message_thread ladder it
                # calls) already warns at the point the read actually failed — the missing route,
                # the unreadable sender, the missing self-name. Restating it here as a second
                # warning double-counts one failure into two grouped issues (#1750, the same
                # `invite_to_connect`/`_add_connect_note` pattern in utilities/CLAUDE.md).
                log_debug(f"Follow-up skipped: could not read the thread with "
                          f"{f.get('first_name') or f['profile_url']} — deferring to the next run",
                          user_id=user_id, action_type=LogActionType.FOLLOWUP,
                          task_name="process_user_followups")
                unreadable_reads = int(f.get("unreadable_reads") or 0) + 1
                backoff_hours = _unreadable_backoff_hours(unreadable_reads)
                # Aware UTC on purpose: the repository normalizes it through `to_naive_utc`, the one
                # storage-side conversion every `dm_followups.due_at` writer and `get_due_followups`
                # itself go through, so this row cannot land on a different clock than the query
                # that will pick it up (docs/timezone-contract.md).
                backoff_due_at = (datetime.now(timezone.utc) + timedelta(hours=backoff_hours)
                                  if backoff_hours else None)
                # Write FIRST, warn only if it landed. The decision above is made from the count
                # read at the start of this run, so a write that matched nothing means the counter
                # never advanced — warning anyway would announce a backoff that did not happen, and
                # then announce it again on every following beat.
                if not record_unreadable_read(f["id"], due_at=backoff_due_at):
                    log_error(f"Could not count an unreadable read against follow-up {f['id']} — "
                              f"the row keeps its old due_at and will be re-read next run",
                              user_id=user_id, action_type=LogActionType.FOLLOWUP,
                              task_name="process_user_followups")
                elif backoff_due_at is not None:
                    # WARNING (not DEBUG): this is new information the miss above doesn't carry —
                    # a thread that has now stayed unreadable across `unreadable_reads` separate
                    # runs, not one read failing. Recurrence escalates this to ERROR and files ONE
                    # grouped issue (utilities/CLAUDE.md) instead of 48 silent SUCCESS runs a day.
                    # Once per backoff STEP, not per read: the step only grows when the streak does.
                    #
                    # The follow-up itself arriving up to a cap-length (48h) late is the accepted
                    # cost. A nurture touch is not time-critical — the alternative is either sending
                    # blind (#731's one irreversible mistake) or dropping the row entirely.
                    log_warning(f"Follow-up thread with {f.get('first_name') or f['profile_url']} "
                                f"has been unreadable for {unreadable_reads} reads in a row — "
                                f"backing off to a read every {backoff_hours}h instead of every "
                                f"run (row stays 'pending'; #731's UNKNOWN still never sends)",
                                user_id=user_id, action_type=LogActionType.FOLLOWUP,
                                task_name="process_user_followups")
                continue
            # Anything below here is a state check_dm_replied ACTUALLY read, so the row's
            # consecutive-UNKNOWN streak is over and must not carry into a later unreadable spell
            # (#1815). Explicit rather than folded into `mark_followup`: every branch below happens
            # to end the row today, but a future readable branch that leaves it 'pending' would
            # otherwise inherit a stale streak and back off a perfectly readable thread by 48h.
            if int(f.get("unreadable_reads") or 0):
                reset_unreadable_reads(f["id"])
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
                mark_followup(f["id"], FollowupStatus.STOPPED)
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
                mark_followup(f["id"], FollowupStatus.STOPPED)
                continue
            msg = build_dm_from_template(user_id, f["event_type"], f["first_name"], my_profile, step=f["next_step"])
            if not msg:
                mark_followup(f["id"], FollowupStatus.STOPPED)
                continue
            send_private_dm.apply_async(kwargs={"user_id": user_id, "profile_url": f["profile_url"], "message": msg})
            insert_new_log(user_id=user_id, action_type=LogActionType.FOLLOWUP, result=LogResultType.SUCCESS,
                           post_url=f["profile_url"], message=msg)
            mark_followup(f["id"], FollowupStatus.SENT)
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


@shared_task.task(name='cqc_lem.app.run_automation.automate_appreciation_dms_for_user',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
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
                # DEBUG on both branches: the re-queue loop's own bookkeeping, and the duration
                # running out IS its exit condition.
                log_debug(f"Loop duration reached. Stopping {current_function_name} task...")
            else:
                # Change the value of the loop_for_duration parameter
                kwargs['loop_for_duration'] = new_loop_for_duration
                # Add our function call back to the task queue
                log_debug(f"Adding {current_function_name} back to queue for {future_forward} seconds in the future...")
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
        # DEBUG: the viewer walk revisits the same activities on consecutive runs, so this is the
        # dedup working rather than anything going wrong.
        log_debug("Already commented on this post. Skipping...", user_id=user_id,
                  action_type="comment")
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


@shared_task.task(name='cqc_lem.app.run_automation.automate_profile_viewer_engagement',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
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

    # `result` is deliberately NOT seeded here: every path out of the block below assigns it (the
    # completion line, or the `except Exception` handler), so a seed value is dead — CodeQL's
    # `py/multiple-definition`, latent in `run_automation` and surfaced by the move because the
    # alert is keyed by location (#1154).
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
                # DEBUG on both branches: the re-queue loop's own bookkeeping, and the duration
                # running out IS its exit condition.
                log_debug(f"Loop duration reached. Stopping {current_function_name} task...")
            else:
                # Change the value of the loop_for_duration parameter
                kwargs['loop_for_duration'] = new_loop_for_duration
                # Add our function call back to the task queue
                log_debug(f"Adding {current_function_name} back to queue for {future_forward} seconds in the future...")
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


def _profile_viewer_dm_blocked(user_id: int, profile_url: str, viewer_name: str) -> bool:
    """True when a queued profile-viewer DM could not be filed for this person (issue #1137).

    Asked BEFORE the message is written as well as before the insert, because an open draft is the
    STEADY STATE for this lane, not the exception: the walk re-lists the same viewer every loop for
    as long as they sit inside the lookback window, and only the first visit can queue anything.
    Answering this late would render the template and run the history-dedup call on every later
    visit, forever, for a draft that can never be written.
    """
    if (has_open_scheduled_dm(user_id, profile_url, source=SCHEDULED_DM_SOURCE_PROFILE_VIEWER)
            or has_open_scheduled_dm(user_id, profile_url, source=SCHEDULED_DM_SOURCE_NURTURE)
            or has_open_scheduled_dm(user_id, profile_url, source=SCHEDULED_DM_SOURCE_ARTIFACT)):
        # DEBUG: one open draft per conversation is the designed rule, and the analytics page lists
        # the same viewer on consecutive runs — an expected no-op, not a missed opportunity.
        log_debug(f"Profile viewer: {viewer_name} already has a queued draft; not queueing another",
                  user_id=user_id, action_type="dm", task_name="engage_with_profile_viewer")
        return True
    return False


def _queue_profile_viewer_dm(user_id: int, profile_url: str, message: str, first_name: str,
                             viewer_name: str) -> Optional[int]:
    """File a cold profile-viewer DM as a PENDING `scheduled_dms` row (issue #1137).

    Returns the row id, or None when nothing was queued. The one-open-draft rule is SHARED with the
    other two auto-drafting mechanics (#485 nurture, #624 artifact) for the reason it was shared
    between those two: two queued messages read as spam to the one person receiving them, whichever
    mechanic wrote them. This is the coldest of the three, so it is the one that yields.
    """
    if _profile_viewer_dm_blocked(user_id, profile_url, viewer_name):
        return None
    dm_id = insert_scheduled_dm(user_id, profile_url, message,
                                datetime.now(timezone.utc).replace(tzinfo=None),
                                recipient_name=first_name or None,
                                status=ScheduledDmStatus.PENDING,
                                source=SCHEDULED_DM_SOURCE_PROFILE_VIEWER)
    if not dm_id:
        log_warning(f"Profile viewer: drafted a DM for {viewer_name} but the scheduled_dms insert "
                    f"failed — nothing will reach them", user_id=user_id, action_type="dm",
                    task_name="engage_with_profile_viewer")
        return None
    log_info(f"Profile viewer: queued a DM to {viewer_name} for approval", user_id=user_id,
             action_type="dm", task_name="engage_with_profile_viewer")
    return dm_id


def _profile_viewer_connect_blocked(user_id: int, profile_url: str, viewer_name: str,
                                    prefs: dict) -> bool:
    """True when a queued profile-viewer invite could not be filed for this person (issue #1137).

    Asked BEFORE the note is written as well as before the insert. The `get_requested_person_keys`
    half is PERMANENT — one request per person ever — so after the first visit every later visit by
    the same viewer is a guaranteed no-op, and the walk re-lists them for as long as they stay in
    the lookback window. Answering it only at insert time would spend an activity summary, a
    personalised draft and a refinement pass on every one of those visits, on a note that can never
    be filed.
    """
    try:
        cap = max(0, int(prefs.get("max_invites_per_day") or 0))
    except (TypeError, ValueError):
        cap = 0
    if cap - count_invites_sent_today(user_id) - count_open_connection_requests(user_id) <= 0:
        # DEBUG: the queue holding a day's worth of invites is the cap working, not a fault. The
        # viewer is skipped rather than deferred — `has_engaged_url_with_x_days` already ends this
        # visit, and the analytics page lists them again while they keep visiting.
        log_debug(f"Profile viewer: the invite budget is already spoken for; not queueing "
                  f"{viewer_name}", user_id=user_id, action_type="connection_targeting",
                  task_name="engage_with_profile_viewer")
        return True
    key = person_key(clean_person_name(viewer_name) or None, profile_url)
    if key and key in get_requested_person_keys(user_id):
        # DEBUG: an expected no-op — the viewer list repeats people, and one invite per person is
        # the rule, not a failure to act.
        log_debug(f"Profile viewer: {viewer_name} already has a connection request on file",
                  user_id=user_id, action_type="connection_targeting",
                  task_name="engage_with_profile_viewer")
        return True
    return False


def _queue_profile_viewer_connect(user_id: int, profile_url: str, message: str,
                                  viewer_name: str, prefs: dict) -> Optional[int]:
    """File a cold profile-viewer invite as a PENDING `connection_requests` row (issue #1137).

    Returns the row id, or None when nothing was queued. ONE request per person ever:
    `get_requested_person_keys` is the same dedup the nightly sourcing scan uses, so someone who
    declined — or who is still sitting in the queue — is never re-filed by a later visit. Approval
    and sending stay on #398's existing beat and review surface; no new table, no new UI.

    Bounded by the SAME budget `_connect_target_budget` and `roster_connect_budget` spend, for the
    reason they both spend it: an OPEN request is already counted against `max_invites_per_day` by
    `count_open_connection_requests`, and a pending row never ages out. Filing without that check is
    what turns a lane nobody approves into a permanent zero for the other two — a backlog of cap-many
    unapproved viewer drafts would silently stop the #486 sourcing scan and the #979 roster ladder
    from filing anything, forever. Direct dispatch never had that effect: an invite it sent counted
    only for the day it was sent.
    """
    if _profile_viewer_connect_blocked(user_id, profile_url, viewer_name, prefs):
        return None
    name = clean_person_name(viewer_name) or None
    request_id = insert_connection_request(user_id, profile_url, message=message,
                                           recipient_name=name,
                                           status=ConnectionRequestStatus.PENDING,
                                           source=CONNECTION_REQUEST_SOURCE_PROFILE_VIEWER)
    if not request_id:
        log_warning(f"Profile viewer: drafted a connection request for {viewer_name} but the "
                    f"connection_requests insert failed — nothing will reach them", user_id=user_id,
                    action_type="connection_targeting", task_name="engage_with_profile_viewer")
        return None
    log_info(f"Profile viewer: queued a connection request to {viewer_name} for approval",
             user_id=user_id, action_type="connection_targeting",
             task_name="engage_with_profile_viewer")
    return request_id


@shared_task.task(name='cqc_lem.app.run_automation.engage_with_profile_viewer',
                  bind=True, base=QueueOnce, once={'graceful': True, 'keys': ['user_id', 'viewer_url']},
                  reject_on_worker_lost=True, rate_limit='2/m', queue='se_outreach')
def engage_with_profile_viewer(self, user_id: int, viewer_url, viewer_name):
    """Engage ONE person who viewed the user's profile, branching on whether we are already connected.

    A 1st-degree viewer gets a comment on ONE recent activity (at most one, then the walk stops) and
    a templated DM; anyone else gets a personalised connection request instead — you cannot DM a
    stranger, so the two branches are different actions, not the same action at different depths.

    Both of those are COLD contact, and since issue #1137 both are approval-gated by the ONE
    `profile_viewer_dm_auto_send` preference: OFF (the default) files a PENDING `scheduled_dms` /
    `connection_requests` row for the operator instead of dispatching, ON keeps dispatching directly.
    The comment half is never gated — commenting on someone's post is public, reversible engagement.

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
        # DEBUG: the docstring says the analytics page lists the same viewer on consecutive runs,
        # which makes this the documented expected repeat, not a skipped opportunity.
        log_debug(f"Already engaged with {viewer_name} today. Skipping...", user_id=user_id,
                  task_name="engage_with_profile_viewer")
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

                acting_user_id = get_user_id(my_profile.email)
                # ONE toggle for BOTH branches (issue #1137). A visit resolves to exactly one of
                # them — we are connected to this viewer or we are not — so "DM a stranger" and
                # "invite a stranger" are the same decision seen from two sides. OFF (the default)
                # files an approval-gated row on each side instead of dispatching; ON is the
                # pre-#1137 behaviour, unchanged.
                prefs = get_engagement_preferences(acting_user_id) or {}
                auto_send = bool(prefs.get("profile_viewer_dm_auto_send"))

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

                    # The gated path asks whether a draft could be FILED before it pays to write
                    # one: an open draft is this lane's steady state, and the walk re-lists the
                    # same viewer every loop. Direct dispatch skips the question — it has no queue
                    # to collide with, and that is the pre-#1137 behaviour the toggle restores.
                    if not able_to_comment and not auto_send and _profile_viewer_dm_blocked(
                            acting_user_id, profile_url_str, viewer_name):
                        result = f"Did not queue a DM to {viewer_name}"

                    elif not able_to_comment:
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


                        if message and auto_send:
                            # Send actual DM
                            kwargs = {'user_id': acting_user_id,
                                      'profile_url': profile_url_str,
                                      'message': message}
                            send_private_dm.apply_async(kwargs=kwargs)
                            enqueue_next_followup(acting_user_id, profile_url_str, first_name, "profile_viewer", 0)
                            result = f"Profile Viewer Engagement Completed. Sent DM to {viewer_name}"
                            engagement_successful = True
                        elif message:
                            # The ladder is NOT started here: `send_scheduled_dm` starts it when the
                            # DM actually lands, so a draft nobody approves never queues follow-ups
                            # for a conversation that never began.
                            dm_id = _queue_profile_viewer_dm(acting_user_id, profile_url_str, message,
                                                             first_name, viewer_name)
                            if dm_id:
                                result = (f"Profile Viewer Engagement Completed. Queued a DM to "
                                          f"{viewer_name} for approval")
                                engagement_successful = True
                            else:
                                result = f"Did not queue a DM to {viewer_name}"
                        else:
                            result = f"Message already sent to {viewer_name}"
                elif not auto_send and _profile_viewer_connect_blocked(
                        acting_user_id, str(profile.profile_url), viewer_name, prefs):
                    # Same question, asked before the note is written rather than after: this
                    # branch's dedup is PERMANENT (one request per person, ever), so from the
                    # second visit onwards drafting one is guaranteed waste — an activity summary,
                    # a personalised message and a refinement pass, per visit, thrown away.
                    result = f"Did not queue a connection request to {viewer_name}"
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

                    if auto_send:
                        # Send connection request with this message
                        kwargs = {'user_id': acting_user_id,
                                  'profile_url': str(profile.profile_url),
                                  'message': refined_response}
                        invite_to_connect.apply_async(kwargs=kwargs)
                        result = f"Profile Viewer Engagement Completed. Sent Connection Request to {viewer_name}"
                        engagement_successful = True
                    else:
                        # `connection_requests` (#398) already has the beat that sends an APPROVED
                        # row and the review surface that approves one — this lane files into it
                        # rather than growing a second queue.
                        request_id = _queue_profile_viewer_connect(
                            acting_user_id, str(profile.profile_url), refined_response, viewer_name,
                            prefs)
                        if request_id:
                            result = (f"Profile Viewer Engagement Completed. Queued a connection "
                                      f"request to {viewer_name} for approval")
                            engagement_successful = True
                        else:
                            result = f"Did not queue a connection request to {viewer_name}"
            else:
                # WARNING: the viewer's profile scrape came back with nothing, so this whole
                # engagement silently does nothing. Symmetric with get_my_profile's
                # "scrape returned nothing" — once is SDUI noise, repeatedly is drift.
                log_warning("Profile scrape returned nothing for this viewer — skipping them",
                            user_id=user_id, action_type="scrape",
                            task_name="engage_with_profile_viewer")
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

    `needs_images=True` on the session (#1774): this composer lives on `/messaging/*`, and the
    proxy bandwidth saver's blocked images stop that surface's fastboot app from ever mounting —
    every DM send lane (private, scheduled, catch-up, nurture, appreciation) shares this function,
    so this is the ONE place the exemption needs to be set.
    """
    user_email, user_password = get_user_password_pair_by_id(user_id)

    driver, wait = get_driver_wait_pair(session_name='Private DM', user_id=user_id, needs_images=True)

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


@shared_task.task(name='cqc_lem.app.run_automation.send_private_dm',
                  bind=True, base=QueueOnce, once={'graceful': True}, reject_on_worker_lost=True,
                  rate_limit='2/m', queue='se_outreach')
def send_private_dm(self, user_id: int, profile_url: str, message: str):
    """Send dm message to a profile. Must be a 1st connection"""
    dm_sent = send_dm_now(user_id, profile_url, message)
    result = "DM Sent Successfully" if dm_sent else "DM Failed"
    # A task wrapper is a caller (#1038): every way send_dm_now can fail already logged itself —
    # ERROR with exc= for a raise, DEBUG for a composer it could not address — and wrote the
    # durable FAILURE row, so restating the failure here is DEBUG. The success stays INFO.
    (log_info if dm_sent else log_debug)(result, user_id=user_id, action_type="dm",
                                         task_name="send_private_dm")
    return result


@shared_task.task(name='cqc_lem.app.run_automation.send_scheduled_dm',
                  bind=True, base=QueueOnce, once={'graceful': True, 'keys': ['dm_id']},
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
    if dm_sent and dm.get("source") == SCHEDULED_DM_SOURCE_PROFILE_VIEWER:
        # The direct-dispatch branch starts the 'profile_viewer' ladder the moment it sends, so the
        # gated branch has to start it the moment it LANDS (issue #1137) — otherwise gating the lane
        # would also silently drop its follow-ups and the reply check that turns a reply into a
        # nurture draft. Started here and not at draft time: a draft nobody approves must never
        # queue follow-ups for a conversation that never began.
        enqueue_next_followup(user_id, dm["recipient_profile_url"],
                              dm.get("recipient_name") or "", "profile_viewer", 0)
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


@shared_task.task(name='cqc_lem.app.run_automation.send_lead_response',
                  bind=True, base=QueueOnce, once={'graceful': True, 'keys': ['signal_id']},
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
        # Same header-anchor reader as the reply sweep (#1091) — the naive first-/in/-link read
        # named nobody on cards whose avatar anchor comes first, and dropped them silently here too.
        author = comment_author_identity(driver, comment)
        if not author.name or not author.profile_url:
            continue
        signals.append(CandidateSignal(person_name=author.name,
                                       person_profile_url=author.profile_url,
                                       source=SOURCE_ADJACENT_POST, context_url=post_url,
                                       context_author=author_name, occurred_at=now,
                                       connection_degree=author.connection_degree))
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


@shared_task.task(name='cqc_lem.app.run_automation.scan_connection_candidates',
                  bind=True, base=QueueOnce,
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


@shared_task.task(name='cqc_lem.app.run_automation.process_outreach_funnel',
                  bind=True, base=QueueOnce,
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


@shared_task.task(name='cqc_lem.app.run_automation.scan_outreach_funnel_targets',
                  bind=True, base=QueueOnce,
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
# The current SDUI render (#1774, live 2026-08-31 grounding: the read-only `--catchup-cards` probe
# matched NEITHER locator above on 10/10 classified cards) carries the default response on the
# card's own "Message" anchor instead of a chip or a dialog-opening button — its `body` query param
# IS the full congratulations LinkedIn drafted, readable with zero clicks and zero dialogs. Kept
# separate from `_CATCHUP_SUGGESTED_TEXT_LOCATORS` because it needs URL parsing, not element text.
_CATCHUP_MESSAGE_LINK_LOCATORS = [
    (By.CSS_SELECTOR, "a[href*='/messaging/compose/']"),
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


# The "Message <name>: <text>" shape LinkedIn's own anchor label carries — the fallback source when
# the `body` query param is unreadable (a relative href with no query at all, say).
_CATCHUP_MESSAGE_ARIA_RE = re.compile(r"^message\s+.+?:\s*(.+)$", re.IGNORECASE)


def _card_message_link_suggested_text(card: WebElement) -> str:
    """LinkedIn's default response as carried on the card's own "Message" anchor (#1774).

    The current SDUI render drops the chip/dialog affordance `_card_suggested_message` was written
    against — grounded live 2026-08-31: `_CATCHUP_SUGGESTED_TEXT_LOCATORS` and
    `_CATCHUP_MESSAGE_TRIGGER_LOCATORS` matched NEITHER on any classified card — and instead links
    straight to `/messaging/compose/` with the full congratulations already in the `body` query
    param. Reading it needs no click and opens nothing; the anchor's own aria-label
    ("Message Jane: Congrats on...") is the fallback for a link LinkedIn ever renders without one.
    """
    el = _first_in_card(card, _CATCHUP_MESSAGE_LINK_LOCATORS)
    if el is None:
        return ""
    try:
        href = el.get_attribute("href") or ""
    except (StaleElementReferenceException, NoSuchElementException):
        return ""
    # The URL is untrusted page content, not a value this module controls — a malformed href must
    # read as "no draft found" like every other miss here, never raise past the scraper.
    try:
        body = (parse_qs(urlparse(href).query).get("body") or [""])[0]
        if body:
            return _clean_suggested_message(body)
    except Exception:
        # An empty/malformed `body` falls through to the aria-label read below — never raise.
        pass
    try:
        aria_match = _CATCHUP_MESSAGE_ARIA_RE.match((el.get_attribute("aria-label") or "").strip())
        return _clean_suggested_message(aria_match.group(1)) if aria_match else ""
    except (StaleElementReferenceException, NoSuchElementException, TypeError, AttributeError):
        return ""


def _card_suggested_message(card: WebElement) -> str:
    """LinkedIn's suggested congratulations as rendered ON the card, if any. Read-only — no clicking.

    Tries the "Message" anchor's own `body` param first (#1774) — it needs no click at all — then
    falls back to the older quick-reply-chip shape in case LinkedIn rotates back to it.
    """
    link_text = _card_message_link_suggested_text(card)
    if link_text:
        return link_text
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


@shared_task.task(name='cqc_lem.app.run_automation.automate_catchup_touches',
                  bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
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
        # DEBUG on all three throttle deferrals in this lane: an open breaker is working behaviour,
        # reported where it is DETECTED (rate_limit.mark_rate_limited), and the lane retries on the
        # next rotation by design — the same call send_roster_connect_invite makes.
        log_debug(f"automate_catchup_touches deferred (throttled): {e}", user_id=user_id,
                  task_name=task_name)
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
        log_debug(f"automate_catchup_touches deferred mid-scrape (throttled): {e}",
                  user_id=user_id, task_name=task_name)
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


@shared_task.task(name='cqc_lem.app.run_automation.send_catchup_touch',
                  bind=True, base=QueueOnce, once={'graceful': True, 'keys': ['touch_id']},
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
        log_debug(f"send_catchup_touch: throttled, deferring {touch_id}: {e}", user_id=user_id,
                  action_type="dm", task_name="send_catchup_touch")
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
