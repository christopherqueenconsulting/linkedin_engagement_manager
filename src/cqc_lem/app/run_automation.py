import hashlib
import inspect
import json
import math
import os
import re
import random
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from celery_once import QueueOnce
from cqc_lem.app.my_celery import app as shared_task
from cqc_lem.utilities.ai.content_framework import select_blueprint
from cqc_lem.utilities.ai.ai_helper import generate_ai_response, get_ai_message_refinement, summarize_recent_activity, \
    ai_check_message_history, post_is_relevant, generate_newsletter_edition, generate_group_post, \
    generate_thread_reply, generate_comment_reply_followup, generate_seed_comment, choose_post_reaction, \
    get_or_create_profile_synthesis, generate_lead_response, generate_nurture_dm, \
    synthesize_profile, lint_repaired, generate_second_wave_comment
from cqc_lem.utilities.ai.lead_intent import detect_lead_signals
from cqc_lem.utilities.ai.dm_nurture import classify_reply_intent, is_stop_intent, nurture_delay_hours
from cqc_lem.utilities.connection_targeting import CandidateSignal, ScoredCandidate, \
    CONNECT_NOTE_LIMIT, SOURCE_ADJACENT_POST, SOURCE_OWN_POST, default_connect_note, \
    rank_candidates, target_terms_from_prefs
from cqc_lem.utilities.lead_scoring import person_key
from cqc_lem.utilities.ai.content_alignment import humanize_text, split_link_for_first_comment, \
    append_link_to_comment, resolve_artifact_delivery, ARTIFACT_KIND_LEAD_MAGNET
from cqc_lem.utilities.date import convert_viewed_on_to_date
from cqc_lem.utilities.db import get_user_password_pair_by_id, get_user_id, insert_new_log, LogActionType, \
    CONNECTION_REQUEST_SENT_MESSAGE, ALREADY_CONNECTED_MESSAGE, NO_CONNECT_BUTTON_MESSAGE, \
    INVITE_NOT_SENT_MESSAGE, CONNECT_NOTE_MAX_CHARS, \
    get_engagement_preferences, count_comments_today, get_recent_engagers, upsert_engager, \
    get_newsletter_settings, mark_newsletter_published, record_newsletter_subscriber_stat, \
    get_newsletter_edition, mark_edition_published, mark_edition_failed, \
    upsert_user_group, get_enabled_group_ids, record_group_post, record_group_post_run, record_post_stats, \
    get_recent_posted_post_ids, get_uncaptured_posted_post_ids, \
    get_shipped_variant_keys, \
    get_lead_magnet_settings, has_received_lead_magnet, record_lead_magnet_sent, \
    LogResultType, has_user_commented_on_post_url, get_post_url_from_log_for_user, get_post_message_from_log_for_user, \
    claim_post_for_comment, mark_post_commented, mark_post_reacted, release_post_claim, has_commented_post, \
    get_recent_navigable_commented_posts, get_comment_followup, record_comment_followup, \
    count_followup_replies_today, update_commented_post_key, get_recent_commented_rows_with_text, \
    has_engaged_url_with_x_days, get_post_content, get_post_video_url, update_db_post_status, PostStatus, PostType, \
    update_db_post_content, update_db_post_first_comment_link, get_post_first_comment_link, \
    get_dm_history_for_profile, get_post_status, get_user_blog_url, get_post_type, get_carousel_slides, \
    get_dm_template, enqueue_followup, get_due_followups, mark_followup, stop_followups_for_profile, \
    set_profile_synthesis, get_duplicate_comment_posts, get_recent_comment_texts, count_dms_sent_today, \
    get_approved_outreach_targets, update_outreach_target, update_outreach_target_status, \
    OutreachStage, OutreachStatus, insert_outreach_target, get_outreach_target_by_url, \
    count_open_outreach_targets, \
    insert_lead_signal, has_lead_signal, get_lead_signal, update_lead_signal, \
    LeadSignalSource, LeadSignalChannel, LeadSignalStatus, \
    insert_scheduled_dm, has_open_scheduled_dm, count_scheduled_dms_created_today, \
    ScheduledDmStatus, SCHEDULED_DM_SOURCE_NURTURE, SCHEDULED_DM_SOURCE_ARTIFACT, \
    insert_connection_request, count_open_connection_requests, get_requested_person_keys, \
    get_engager_candidates, get_profile_facts, count_invites_sent_today, ConnectionRequestStatus, \
    CatchupTouchStatus, insert_catchup_touch, has_catchup_touch, get_catchup_touch, \
    update_catchup_touch_status, count_catchup_touches_sent_today, max_catchup_touches_allowed, \
    get_engagement_targets, record_target_engagement, ENGAGEMENT_TARGET_WEEKLY_DEFAULT, \
    record_follower_stat, get_linkedin_profile_url_by_user_id, \
    get_comment_outcome_targets, record_comment_outcome, \
    count_user_comments_on_post_url, get_post_age_minutes, get_story_bank_entries, \
    record_story_bank_use
from cqc_lem.utilities.audience_stats import parse_follower_count, parse_connection_count, \
    parse_profile_views, parse_search_appearances
from cqc_lem.utilities.engagement_window import record_pre_post_run
from cqc_lem.utilities.ai import story_bank as _story_bank
from cqc_lem.utilities import golden_hour as _golden
from cqc_lem.utilities.human_pacing import pace_read, record_action, remaining_actions, \
    engagement_caps_from_prefs, \
    ACTION_COMMENT, ACTION_DM, ACTION_INVITE, ACTION_REPLY
from cqc_lem.utilities.linkedin.article_editor import fill_article_editor
from cqc_lem.utilities.linkedin.company_page_inviter import automate_invitations, \
    plan_daily_invites, INVITE_STATUS_FAILED, INVITE_STATUS_PAUSED, INVITE_STATUS_SESSION_FAILED
from cqc_lem.utilities.linkedin.helper import login_to_linkedin, get_my_profile, get_linkedin_profile_from_url, \
    load_profile_for_user, clean_person_name, connection_degree, is_first_degree
from cqc_lem.utilities.linkedin.message_thread import ThreadState, name_matches, \
    open_message_thread, read_last_message, read_last_sender, resolve_self_name
from cqc_lem.utilities.linkedin.poster import share_on_linkedin, share_carousel_on_linkedin, \
    share_document_on_linkedin, comment_on_linkedin_post, object_urn_from_post_url
from cqc_lem.utilities.linkedin.profile import LinkedInProfile
from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited, _redis_client, \
    acquire_run_lock, release_run_lock, commenting_hold_reason, is_commenting_held, \
    is_automation_paused, automation_pause_reason
from cqc_lem.utilities.linkedin_formatter import normalize_public_text
from cqc_lem.utilities.logger import myprint, log_error, log_info, log_warning, log_debug
from cqc_lem.utilities.observability import track_post_outcome, track_audience_snapshot, \
    track_comment_outcome, track_golden_hour_report, track_company_page_invite_run, \
    track_catchup_run, track_feed_scan, attribute_llm_cost, llm_attribution, \
    FEATURE_COMMENT, FEATURE_CONTENT, FEATURE_DM
from cqc_lem.utilities.env_constants import INLINE_REACTIONS_ENABLED, MAX_WAIT_RETRY
from cqc_lem.utilities.selenium_util import click_element_wait_retry, \
    get_element_wait_retry, get_elements_as_list_wait_stale, getText, close_tab, get_driver_wait_pair, quit_gracefully, \
    wait_for_ajax, find_first, click_first, find_all_first
from dotenv import load_dotenv
from selenium.common import NoSuchElementException, JavascriptException, StaleElementReferenceException, \
    ElementNotInteractableException, WebDriverException, TimeoutException, ElementClickInterceptedException
from selenium.webdriver import ActionChains, Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

# Load .env file
load_dotenv()

# Global flag to indicate when to stop the thread
stop_all_thread = threading.Event()
time_remaining_seconds = 0


def countdown_timer(seconds):
    global stop_all_thread
    global time_remaining_seconds
    time_remaining_seconds = seconds
    while seconds > 0 and not stop_all_thread.is_set():
        mins, secs = divmod(seconds, 60)
        timer = f'Time left: {mins:02d}:{secs:02d}'
        sys.stdout.write('\r' + timer)
        sys.stdout.flush()
        time.sleep(1)
        seconds -= 1
        time_remaining_seconds = seconds
    sys.stdout.write('\rTime left: 00:00\n')
    sys.stdout.flush()
    stop_all_thread.set()  # Set the flag to stop other threads


def get_time_remaining_seconds():
    global time_remaining_seconds
    return time_remaining_seconds


def get_time_remaining_minutes():
    return get_time_remaining_seconds() // 60


def navigate_to_feed(driver, wait):
    # Check to see if driver url is not already on feed
    if "feed" not in driver.current_url:
        # Navigate to LinkedIn home feed
        driver.get("https://www.linkedin.com/feed/")
        wait_for_ajax(driver)

    # SDUI removed the '//button/hr' sort control this used to click, so the legacy sort block here
    # raised (and paged) on every run. _switch_feed_to_recent owns the sort now: resilient selectors,
    # best-effort, warns instead of erroring when the control isn't there.
    _switch_feed_to_recent(driver, wait)


def get_feed_posts(driver, wait, num_posts=10):
    posts = []

    # Find the posts in the feed
    post_element_xpath = '//div[contains(@data-id, "urn:li:activity")]'
    post_elements = get_elements_as_list_wait_stale(wait, post_element_xpath, "Finding Posts in Feed")

    if len(post_elements) == 0:
        print(" No posts found in feed.")

    while len(post_elements) < num_posts:
        # Scroll to the bottom of the page to load more posts
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")

        # Wait for the posts to load
        time.sleep(5)

        # Find the posts in the feed
        new_post_elements = get_elements_as_list_wait_stale(wait, post_element_xpath, "Finding New Posts in Feed")

        if len(new_post_elements) == len(post_elements):
            # No new posts. Exit the loop
            break

        post_elements = new_post_elements

    # Limit to the number of posts we want
    for post in post_elements[:num_posts]:
        # Get the link to the post
        post_link = 'https://www.linkedin.com/feed/update/' + post.get_attribute('data-id')

        posts.append({
            'link': post_link,

        })

    return posts


# Reading/thinking delays now come from utilities/human_pacing.py (issue #626) — one engine, with a
# floor no human beats and a ceiling that keeps the sleep inline-safe.


def simulate_writing_time(content):
    # Simulate a human writing time (around 5 characters per second)
    char_count = len(content)
    writing_time = char_count / 5
    return round(writing_time)


def emoji_to_ue_string(emoji):
    """Converts an emoji to its equivalent escaped sequence."""
    return emoji.encode('unicode_escape').decode('ascii')


def clear_text_from_element(element: WebElement):
    # Select All
    element.send_keys(Keys.CONTROL + "a")
    # Delete what is selected
    element.send_keys(Keys.DELETE)


def simulate_typing(driver: WebDriver, editable_element: WebElement, text, allow_pauses: bool = True):
    # Simulate typing the comment
    myprint("Typing Text...")
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

    myprint("Finished Typing!")


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'keys': ['user_id', 'post_link']},
                  reject_on_worker_lost=True, rate_limit='4/m', queue='se_engage')
def comment_on_post(self, user_id: int, post_link: str, comment_text: str):
    """Post a comment to the given post link"""

    # Check the database logs / claim ledger to make sure user hasn't already commented here.
    if has_user_commented_on_post_url(user_id, post_link) or has_commented_post(user_id, post_link):
        myprint("User has already commented on this post. Skipping...")
        return "User has already commented on this post. Skipping..."

    # Atomically claim before doing any work — a concurrent worker with the same post_link loses
    # here and backs off (belt-and-suspenders alongside QueueOnce's user_id+post_link key).
    if not claim_post_for_comment(user_id, post_link):
        myprint("Another task already claimed this post. Skipping...")
        return "Another task already claimed this post. Skipping..."

    driver, wait = get_driver_wait_pair(session_name='Post Comment', user_id=user_id)

    try:

        user_email, user_password = get_user_password_pair_by_id(user_id)

        login_to_linkedin(driver, wait, user_email, user_password)

        # Create an instance of ActionChains
        actions = ActionChains(driver)

        if post_link != driver.current_url:
            # Switch to post url
            driver.get(post_link)

        # Find the comment input area
        comment_box = click_element_wait_retry(driver, wait,
                                               '//div[contains(@class, "comments-comment-texteditor")]//div[@role="textbox"]',
                                               "Finding the Comment Input Area", use_action_chain=True)

        # Move viewport to the comment_box
        actions.scroll_to_element(comment_box).perform()

        # clear the contents of the comment_box
        comment_box.clear()

        # Simulate typing the comment
        simulate_typing(driver, comment_box, comment_text)

        # Sleep so post button shows up
        time.sleep(2)

        method_result = ''

        try:
            # Find and click the post button
            click_element_wait_retry(driver, wait,
                                     '//button[contains(@class, "comments-comment-box__submit-button--cr")]',
                                     "Clicking Post Button", max_retry=1, use_action_chain=True)

            myprint("Added Comment via Post Button")
            method_result = "Added Comment via Post Button"

            # Promote the claim to 'commented' and record the log.
            mark_post_commented(user_id, post_link)
            insert_new_log(user_id=user_id, action_type=LogActionType.COMMENT, result=LogResultType.SUCCESS,
                           post_url=post_link, message=comment_text)

        except NoSuchElementException:
            # If the post button is not found, send a return key to post the comment
            # comment_box.send_keys('\n')
            comment_box.send_keys(Keys.ENTER)
            # Update database with record of comment to this post
            insert_new_log(user_id=user_id, action_type=LogActionType.COMMENT, result=LogResultType.FAILURE,
                           post_url=post_link, message=comment_text)
            myprint("Added Comment via return key. This might not have worked")
            method_result = "Added Comment via return key. This might not have worked"

        try:

            # Get the main like button
            main_like_button = get_element_wait_retry(driver, wait,
                                                      '//button[contains(@aria-label, "Like") and contains(@class,"artdeco-button")]',
                                                      "Finding Main Like Button")

            button_label_options = ['Like', 'Celebrate', 'Insightful', 'Support',
                                    # 'Love', 'Funny' # TODO: Not sure if these are universal for all post
                                    ]

            # TODO: Use AI to get a preferences
            button_to_click_key = random.choice(button_label_options)

            max_retries = 3
            for attempt in range(max_retries):

                # Wait for new elements to appear (adjust time as needed)
                time.sleep(5)  # This is needed for it to become visible
                try:

                    choice_dict = {}

                    # For each key in the button_path_dict, get the element and add it to the choices list
                    for button_label in button_label_options:
                        button = get_element_wait_retry(driver, wait,
                                                        f"//span[contains(@class,'menu')]//button[contains(@aria-label, '{button_label}')]",
                                                        f"Finding {button_label} Button",
                                                        element_always_expected=False, max_try=1)
                        if button:
                            choice_dict[button_label] = button

                    # Get the choice dict keys as list
                    choices = list(choice_dict.keys())

                    # Randomly chose one of the available button options
                    button_to_click_key = random.choice(choices)
                    myprint(f"Clicking {button_to_click_key} Post Reaction")
                    button_to_click = choice_dict[button_to_click_key]
                    # Move to that button and click it
                    # Hover over the main like button
                    actions.scroll_to_element(main_like_button).move_to_element(main_like_button).move_to_element(
                        button_to_click).click().perform()
                    wait_for_ajax(driver)
                    time.sleep(2)
                    myprint("Added Post Reaction")
                    method_result += " | Added Post Reaction"
                    break  # Exit loop if click is successful
                except Exception as e:
                    if attempt < max_retries - 1:
                        myprint(f"Removing {button_to_click_key} from choice options since it failed")
                        button_label_options.remove(button_to_click_key)
                        time.sleep(1)  # Wait a bit before retrying
                    else:
                        log_warning(f"Failed to click {button_to_click_key} post reaction", exc=e, user_id=user_id, post_id=post_link)
                        method_result += f" | Added Post Reaction | Error: {e}"
        except Exception as e:
            log_warning("Error while clicking post reaction", exc=e, user_id=user_id, post_id=post_link)
            method_result += f"Could not add post reaction | Error: {e}"
    except Exception as e:
        # Nothing posted (login/compose failure) — release the claim so a later run can retry.
        release_post_claim(user_id, post_link)
        log_error("Error while posting comment", exc=e, user_id=user_id, post_id=post_link, action_type="comment")
        method_result = f"Error while posting comment: {e}"
    finally:
        quit_gracefully(driver)  # Close the driver

    return method_result


def check_commented(driver, wait, user_id: int = None, post_url: str = None):
    """See if the current open url we've already posted on"""
    already_commented = False

    if post_url and post_url != driver.current_url:
        myprint(f"Navigating To: {post_url}")
        # Switch to post url
        driver.get(post_url)

    # 1. Check against Database (in logs table)
    if user_id and post_url:
        already_commented = has_user_commented_on_post_url(user_id, post_url)

    # 2. Check against LinkedIn Recent Activity Comments
    if not already_commented:

        # See if the current user is in the comments section
        alink = get_element_wait_retry(driver, wait,
                                       '//div[contains(@class,"comments-comment-list__container")]//a[contains(@aria-label,"• You")]',
                                       "Finding Comments Container with 'You' In it", max_try=1,
                                       element_always_expected=False)
        if alink:
            already_commented = True

    return already_commented


# --- SDUI feed engine (LinkedIn's 2026 redesign) -----------------------------------------
# LinkedIn moved the feed to a server-driven-UI framework: the old urn:li:activity data-ids,
# feed-shared-* / comments-comment-* classes and permalink navigation are gone. Posts are now
# anchored by stable data-testid / aria-label attributes and commenting happens INLINE on the
# feed card (no per-post permalink). Verified live 2026-07-03.
# SDUI home feed uses data-testid='expandable-text-box'; classic Group feeds still render posts as
# feed-shared-update-v2 with .update-components-text — include both so group commenting finds posts.
# Content-hash dedup (_feed_post_key) covers any overlap between the two selectors on a page.
_FEED_POST_TEXT_SEL = "[data-testid='expandable-text-box'], .feed-shared-update-v2 .update-components-text"


# XPath 1.0 has no lower-case(), so translate() is the case fold. Every case-insensitive comparison
# against a LinkedIn label goes through it — sort controls, comment actions, reaction anchors —
# because LinkedIn renders 'Most recent' / 'Recent' / 'Comment' / 'Like' with casing that varies by
# surface, and a literal case-sensitive match against any other casing silently never fires.
# Defined HERE, above the first locator chain that uses it: these are module-level constants
# evaluated at import, so a chain declared before them raises NameError on import.
_X_AZ_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_X_AZ_LOWER = "abcdefghijklmnopqrstuvwxyz"


def _x_lower(expression: str) -> str:
    """`expression` case-folded inside an XPath predicate."""
    return f"translate({expression},'{_X_AZ_UPPER}','{_X_AZ_LOWER}')"


_X_LOWER_TEXT = _x_lower("normalize-space()")
_X_LOWER_ARIA = _x_lower("@aria-label")
_X_LOWER_TESTID = _x_lower("@data-testid")


# The comment ACTION button, resolved by several routes at once (issue #816 grounding run).
#
# Live on the current SDUI the button carries NO aria-label — only the visible text "Comment":
#     {"tag": "button", "type": "button", "text": "Comment"}
# so the long-standing `button[aria-label='Comment']` matched ZERO elements on a feed with 9 posts.
# That single anchor was load-bearing in three places (the card walk, the URN-scan boundary and the
# composer opener), which is why one label rotation took out commenting AND reactions at once.
#
# The text route matches the trimmed label EXACTLY. The feed also renders comment-COUNT affordances
# (`{"tag":"div","role":"button","text":"7 comments"}`); a `contains` match would happily return one
# of those, and clicking a count opens the thread rather than the composer.
_COMMENT_ACTION_JS = r"""
const isCommentAction = (b) => {
  if (!b || !b.getAttribute) return false;
  const aria = (b.getAttribute('aria-label') || '').trim().toLowerCase();
  if (aria === 'comment' || aria.startsWith('comment on')) return true;
  const testid = (b.getAttribute('data-testid') || '').toLowerCase();
  const text = (b.textContent || '').trim().toLowerCase();
  if (testid.includes('comment') && text === 'comment') return true;
  return text === 'comment';
};
const commentButton = (root) => {
  if (!root || !root.querySelectorAll) return null;
  for (const b of root.querySelectorAll("button, [role='button']")) {
    if (isCommentAction(b)) return b;
  }
  return null;
};
"""

_CARD_FOR_TEXTBOX_JS = _COMMENT_ACTION_JS + r"""
let el = arguments[0], d = 0;
while (el && d < 15) { if (commentButton(el)) return el; el = el.parentElement; d++; }
return null;
"""


def _card_for_textbox(driver, box):
    """Nearest ancestor of a post's text box that carries its comment action — i.e. the post card.

    Multi-route by necessity: LinkedIn rotates which of aria-label / data-testid / visible text is
    canonical and often keeps several alive at once, so keying on one is a single point of failure
    that fails SILENTLY — a null card is indistinguishable from an empty feed."""
    return driver.execute_script(_CARD_FOR_TEXTBOX_JS, box)


def _post_author_from_card(card) -> str:
    """Author name is embedded in the card's 'Hide post by <Name>' control's aria-label."""
    try:
        label = card.find_element(By.CSS_SELECTOR, "button[aria-label^='Hide post by']").get_attribute("aria-label") or ""
        return label.replace("Hide post by ", "").strip()
    except Exception:
        return ""


def _author_is_me(author: str, my_profile: LinkedInProfile) -> bool:
    """True if a feed card's author is the logged-in user — used to skip reacting/engaging on our
    OWN posts (the reply-to-own-post path handles those separately)."""
    try:
        me = (getattr(my_profile, "full_name", "") or "").strip().lower()
    except Exception:
        me = ""
    return bool(me) and (author or "").strip().lower() == me


# Canonical LinkedIn post identity. The activity/ugcPost/share URN is stable across re-renders;
# it's the only reliable dedup anchor. A content hash is NOT — a "…see more" toggle or our own
# just-posted comment mutates the card's text and yields a different hash for the SAME post,
# which is how feed commenting posted two comments on one post (issue #474).
_URN_RE = re.compile(r"urn:li:(?:activity|ugcPost|share):\d+", re.I)


def _normalize_post_text(content: str) -> str:
    """Collapse the volatile bits of a card's rendered text so the SAME post hashes the same
    across re-renders: drop the 'see more'/'…more' expander tokens and ellipses, collapse all
    whitespace, lowercase. Used only for the no-URN fallback key + the per-run fingerprint."""
    t = (content or "").lower()
    t = re.sub(r"\s*(?:…|\.\.\.)?\s*see\s+more\b", " ", t)
    t = re.sub(r"\s*(?:…|\.\.\.)\s*more\b", " ", t)
    t = t.replace("…", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t


# The URN-less fallback hashes a PREFIX of the body, not all of it: LinkedIn's collapsed card
# truncates a long post (~3 lines) while the expanded render carries the whole thing, so hashing
# everything mints TWO keys for one post across a re-render — which is how #474 recurred as #580.
# Truncation happens well past 120 characters, so a 120-char prefix is byte-identical in both.
_FEED_KEY_PREFIX_CHARS = 120
# Extra shorter prefix for the per-run fingerprint set: it still matches when a render truncates
# even earlier than the canonical prefix (narrow window, aggressive "…more" collapse).
_FEED_FP_PREFIX_CHARS = (60, _FEED_KEY_PREFIX_CHARS)


def _norm_prefix(content: str, limit: int) -> str:
    """Normalized post text cut to `limit` chars on a word boundary — the same prefix for the
    collapsed and the expanded render of one post."""
    norm = _normalize_post_text(content)
    if len(norm) <= limit:
        return norm
    head, sep, _tail = norm[:limit].rpartition(" ")
    return head if sep else norm[:limit]


def _content_digest(author: str, content: str, limit: int) -> str:
    """Short sha1 over the author + a truncation-proof normalized body prefix."""
    payload = f"{(author or '').strip().lower()}|{_norm_prefix(content, limit)}"
    return hashlib.sha1(payload.encode("utf-8", "ignore")).hexdigest()[:20]


def _feed_post_key(author: str, content: str) -> str:
    """Last-resort dedup key when no stable URN/permalink is available. Hashes the author plus a
    NORMALIZED, truncation-proof body prefix (see _norm_prefix) so the collapsed and the expanded
    render of one post produce ONE key."""
    return f"feedpost://{_content_digest(author, content, _FEED_KEY_PREFIX_CHARS)}"


def _feed_content_fingerprints(author: str, content: str) -> "set[str]":
    """Render-stable per-run fingerprints of a post's text at several prefix lengths — a second
    dedup guard so that even on the URN-less fallback path (or when a URN is found on one pass and
    not the next) a re-render can't re-key the post and earn it a second comment."""
    return {f"fp{n}:{_content_digest(author, content, n)}" for n in _FEED_FP_PREFIX_CHARS}


# The activity URN lives in a data-* attribute on the feed-update CONTAINER, which sits ABOVE the
# comment-button ancestor `_card_for_textbox` returns — so scanning only that card's outerHTML found
# nothing on the live 2026 feed and every comment fell back to the content hash (issue #580). Walk
# UP instead, reading each element's OWN attribute values, and stop at the first ancestor spanning
# more than one post card so we can never pick up a SIBLING post's URN. Descendant attributes are
# checked last (a reshare embeds the original post's URN, so containers outrank children).
_URN_SCAN_JS = _COMMENT_ACTION_JS + r"""
const countCommentActions = (root) => {
  if (!root || !root.querySelectorAll) return 0;
  let n = 0;
  for (const b of root.querySelectorAll("button, [role='button']")) { if (isCommentAction(b)) n++; }
  return n;
};
const RE = /urn:li:(?:activity|ugcPost|share):\d+/i;
const attrHit = (el) => {
  if (!el || !el.attributes) return null;
  for (const a of el.attributes) { const m = RE.exec(a.value || ''); if (m) return m[0]; }
  return null;
};
const el = arguments[0];
let hit = attrHit(el);
if (hit) return hit;
let p = el.parentElement, depth = 0;
while (p && depth < 12) {
  // Stop before an ancestor that spans TWO posts. Counting comment actions is the boundary test,
  // so it must use the same multi-route resolver as the card walk — with the old aria-label-only
  // count this hit 0 everywhere and the scan climbed straight past the card into the feed root,
  // returning a neighbouring post's URN.
  if (p.querySelectorAll && countCommentActions(p) > 1) break;
  hit = attrHit(p);
  if (hit) return hit;
  p = p.parentElement; depth++;
}
if (el.querySelectorAll) {
  for (const d of el.querySelectorAll('*')) { hit = attrHit(d); if (hit) return hit; }
}
return null;
"""


def _feed_post_urn_from_card(card, driver=None) -> "str | None":
    """The canonical urn:li:(activity|ugcPost|share):<id> for a feed card. Reads data-* attributes
    on the card, on its ancestors (never past an element that spans more than one post) and on its
    descendants, then falls back to a regex over the card's own HTML. Lowercased URN or None."""
    runner = driver if driver is not None else getattr(card, "parent", None)
    if runner is not None:
        try:
            found = runner.execute_script(_URN_SCAN_JS, card)
        except Exception:
            found = None
        if isinstance(found, str):
            m = _URN_RE.search(found)
            if m:
                return m.group(0).lower()
    try:
        html = card.get_attribute("outerHTML")
    except Exception:
        return None
    if not isinstance(html, str):
        return None
    m = _URN_RE.search(html)
    return m.group(0).lower() if m else None


def _feed_post_identity(card, author: str, content: str, driver=None) -> "tuple[str, str]":
    """(dedup key, key SOURCE) for a feed post. Source is 'permalink' | 'card' | 'hash' — recorded
    on the run so we can confirm live that feed comments key on the stable activity URN and not on
    the volatile content hash (issue #580)."""
    permalink = _post_permalink_from_card(card)
    if permalink:
        m = _URN_RE.search(permalink)
        if m:
            return f"feedurn://{m.group(0).lower()}", "permalink"
    urn = _feed_post_urn_from_card(card, driver=driver)
    if urn:
        return f"feedurn://{urn}", "card"
    return _feed_post_key(author, content), "hash"


def _stable_feed_post_key(card, author: str, content: str, driver=None) -> str:
    """Single canonical dedup key for a feed post, stable across re-renders. Prefers the URN
    (from the permalink anchor OR the card/ancestor data attributes) so permalink-present and
    permalink-absent renders of the same post map to ONE key; only falls back to the normalized
    content hash when no URN can be found."""
    return _feed_post_identity(card, author, content, driver=driver)[0]


def _post_permalink_from_card(card):
    """Real LinkedIn permalink for a feed post, read from its /feed/update/ anchor (the SDUI
    card has no data-urn). Returns a normalized https URL or None."""
    try:
        for a in card.find_elements(By.CSS_SELECTOR, "a[href*='/feed/update/']"):
            href = (a.get_attribute("href") or "").split("?")[0]
            if "/feed/update/" in href:
                return href.rstrip("/") + "/"
        return None
    except Exception:
        return None


# Relative-age units → minutes. The SDUI card shows a token like "3h •", "5d •", "2w •", "10mo •".
_AGE_UNIT_MIN = {"s": 0, "m": 1, "h": 60, "d": 1440, "w": 10080, "mo": 43200, "y": 525600}
_AGE_TOKEN_RE = re.compile(r"^(\d+)\s?(mo|[smhdwy])", re.I)
# LinkedIn renders social counts as "1,234", "1.2K", or "3M" depending on magnitude — parse all
# three. Anchored on the trailing label word so a bare reaction glyph count never masquerades as,
# say, impressions. The separator is horizontal-only (`[^\S\n]`): on the stacked analytics layout
# each count sits on its own line, so allowing \s+ here would let the PREVIOUS row's value bind to
# the next row's label ("Comments\n1\nReposts\n0" → reposts=1). _stacked_counts handles that layout.
_COUNT = r"([\d,]+(?:\.\d+)?[KMBkmb]?)"
_SEP = r"[^\S\n]+"
_COMMENTS_RE = re.compile(_COUNT + _SEP + r"comments?", re.I)
_REACTIONS_RE = re.compile(_COUNT + _SEP + r"(?:reactions?|likes?)", re.I)
_IMPRESSIONS_RE = re.compile(_COUNT + _SEP + r"impressions?", re.I)
# LinkedIn labels shares as "reposts" on the SDUI social bar (older UIs said "shares"); accept both.
_REPOSTS_RE = re.compile(_COUNT + _SEP + r"(?:reposts?|shares?)", re.I)
# Saves surface only in the author's post analytics ("N saves"); 0 when the bar doesn't expose it.
_SAVES_RE = re.compile(_COUNT + _SEP + r"saves?", re.I)

# Live post-analytics capture (/analytics/post-summary/urn:li:activity:…/, owner grab 2026-07-23)
# puts every label and its value in SEPARATE elements — driver.text renders one per line — and the
# two blocks stack in OPPOSITE orders: Discovery hero stats read "72\nImpressions" (value first),
# the Engagement breakdown reads "Reposts\n0" (label first). Matching on whole lines (not a loose
# regex over the blob) is what keeps prose like "Save this checklist" out of the numbers.
_STACKED_VALUE_FIRST = {"impressions"}
_STACKED_LABEL_FIRST = {"reactions": "reactions", "comments": "comments", "reposts": "reposts",
                        "shares": "reposts", "saves": "saves"}
_BARE_COUNT_RE = re.compile(r"^[\d,]+(?:\.\d+)?[KMBkmb]?$")

_COUNT_MULT = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def _parse_count(raw: "str | None") -> int:
    """'1,234' → 1234, '1.2K' → 1200, '3M' → 3000000. 0 on anything unparseable."""
    s = (raw or "").strip().replace(",", "")
    if not s:
        return 0
    mult = _COUNT_MULT.get(s[-1].lower(), 1)
    if mult != 1:
        s = s[:-1]
    try:
        return int(round(float(s) * mult))
    except ValueError:
        return 0


def _post_age_minutes(driver, card) -> "int | None":
    """Minutes since the post was published, from the card's relative timestamp span ('now', '3h',
    '5d', '2w', '10mo'). None if not found — the caller treats unknown age as mid-priority, not top."""
    try:
        token = driver.execute_script(
            "const root=arguments[0];"
            "for(const el of root.querySelectorAll('span,time')){"
            "  const t=(el.innerText||el.textContent||'').trim();"
            "  if(t.length<20 && /^(now|\\d+\\s?(mo|[smhdwy])(\\s*[•·].*)?)$/i.test(t)) return t;"
            "}return null;", card)
    except Exception:
        return None
    if not token:
        return None
    token = token.strip().lower()
    if token.startswith("now"):
        return 0
    m = _AGE_TOKEN_RE.match(token)
    if not m:
        return None
    return int(m.group(1)) * _AGE_UNIT_MIN.get(m.group(2), 60)


def _stacked_counts(text: str) -> dict:
    """Counts for the post-analytics layout, where a label and its value are on adjacent lines.
    Only exact label lines pair up, and only with a neighbour that is a bare count — so a row's
    value can never be read as the next row's, and post body text is ignored."""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    out: dict = {}
    for i, line in enumerate(lines):
        label = line.lower().rstrip(":")
        if label in _STACKED_LABEL_FIRST:
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if _BARE_COUNT_RE.match(nxt):
                out.setdefault(_STACKED_LABEL_FIRST[label], _parse_count(nxt))
        elif label in _STACKED_VALUE_FIRST:
            prev = lines[i - 1] if i else ""
            if _BARE_COUNT_RE.match(prev):
                out.setdefault(label, _parse_count(prev))
    return out


def _post_social_counts(card) -> dict:
    """Best-effort reaction/comment/repost/impression/save counts parsed from the card's social-counts
    bar text. Returns {reactions, comments, reposts, impressions, saves} (0 on miss). Impressions and
    saves show only on the author's own post detail/analytics view; reposts weigh 2× in the
    engagement score (#387); reactions/comments feed the low-weight feed 'activity' scoring signal."""
    zero = {"reactions": 0, "comments": 0, "reposts": 0, "impressions": 0, "saves": 0}
    try:
        text = card.text or ""
    except Exception:
        return dict(zero)

    stacked = _stacked_counts(text)

    def _num(rx, key):
        m = rx.search(text)
        return _parse_count(m.group(1)) if m else stacked.get(key, 0)

    return {"reactions": _num(_REACTIONS_RE, "reactions"), "comments": _num(_COMMENTS_RE, "comments"),
            "reposts": _num(_REPOSTS_RE, "reposts"), "impressions": _num(_IMPRESSIONS_RE, "impressions"),
            "saves": _num(_SAVES_RE, "saves")}


# Feed-post prioritization weights — defaults below, each overridable per-deploy via the matching
# FEED_SCORE_W_* / FEED_RECENCY_HALFLIFE_MIN env var (read at call time, same live-env pattern as
# POST_SIMILARITY_MAX, so ops/tests can tune without a restart). Recency dominates: golden-hour
# posts get 4–10× the algorithmic weight and the author is online to reply — which is the whole
# point (earn a thread).
_SCORE_W_RECENCY = 0.5
_SCORE_W_RELEVANCE = 0.2
_SCORE_W_RECIPROCITY = 0.2
_SCORE_W_ACTIVITY = 0.1
_RECENCY_HALFLIFE_MIN = 180.0  # exp decay: ~1.0 under an hour, ~0.37 at 3h, small by a day


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _recency_score(age_minutes) -> float:
    if age_minutes is None:
        return 0.4  # unknown age → mid-priority, never top
    halflife = _env_float("FEED_RECENCY_HALFLIFE_MIN", _RECENCY_HALFLIFE_MIN)
    return math.exp(-max(0, age_minutes) / max(halflife, 1.0))


def _activity_score(comments: int) -> float:
    # Reward a *forming* thread (still repliable, some signal); mildly penalize 0-traction and
    # heavily penalize oversaturated posts where our comment just gets buried.
    if comments <= 0:
        return 0.4
    if comments <= 15:
        return 1.0
    if comments <= 50:
        return 0.6
    return 0.3


def _passes_hard_excludes(content: str, author: str, prefs: dict) -> bool:
    """Cheap (no-LLM) exclude gate for candidate gathering."""
    if not prefs:
        return True
    text = (content or "").lower()
    auth = (author or "").lower()
    if any(str(k).lower() in text for k in (prefs.get("exclude_keywords") or []) + (prefs.get("exclude_topics") or []) if k):
        return False
    if any(str(a).lower() in auth for a in (prefs.get("exclude_authors") or []) if a):
        return False
    return True


def _literal_relevant(content: str, author: str, prefs: dict) -> bool:
    """Positive relevance signal without an LLM call: no include constraints (everything on-topic
    by config) OR a literal include keyword/author match. Topic-only relevance is confirmed by the
    LLM on the selected post, so this is just the scoring hint."""
    if not prefs:
        return True
    incl_kw = [k for k in (prefs.get("include_keywords") or []) if k]
    incl_auth = [a for a in (prefs.get("include_authors") or []) if a]
    incl_topics = [t for t in (prefs.get("include_topics") or []) if t]
    if not (incl_kw or incl_auth or incl_topics):
        return True
    text = (content or "").lower()
    auth = (author or "").lower()
    if any(str(k).lower() in text for k in incl_kw):
        return True
    return any(str(a).lower() in auth for a in incl_auth)


def _score_feed_post(meta: dict, prefs: dict, engagers: set = None) -> float:
    """Prioritize which feed post to comment on: recency-dominant, then relevance, reciprocity
    (author engaged with us / is a target), and a healthy-activity bonus. Higher = comment first."""
    engagers = engagers or set()
    recency = _recency_score(meta.get("age_minutes"))
    relevance = 1.0 if meta.get("relevant") else 0.6
    author = (meta.get("author") or "").strip().lower()
    incl_auth = {str(a).strip().lower() for a in ((prefs or {}).get("include_authors") or []) if a}
    reciprocal = bool(author) and (author in engagers or any(a and a in author for a in incl_auth))
    reciprocity = 1.0 if reciprocal else 0.0
    activity = _activity_score(meta.get("comments", 0))
    return (_env_float("FEED_SCORE_W_RECENCY", _SCORE_W_RECENCY) * recency
            + _env_float("FEED_SCORE_W_RELEVANCE", _SCORE_W_RELEVANCE) * relevance
            + _env_float("FEED_SCORE_W_RECIPROCITY", _SCORE_W_RECIPROCITY) * reciprocity
            + _env_float("FEED_SCORE_W_ACTIVITY", _SCORE_W_ACTIVITY) * activity)


def _strip_non_bmp(text: str) -> str:
    # First normalize rogue AI typography (em dashes, smart quotes, ellipsis, exotic spaces) to plain
    # ASCII so those tell-tale characters never reach a public comment/post. Then drop non-BMP chars:
    # ChromeDriver's send_keys raises WebDriverException on them (most emoji), so keep the composer typing.
    text = normalize_public_text(text or "")
    return ''.join(c for c in text if ord(c) <= 0xFFFF)


# The SDUI comment/reply composer has NO <form> ancestor, so walk up from the textbox and click
# the enabled submit button whose text is Comment/Post/Reply — excluding the aria-label
# Comment/Reply buttons that OPEN a composer. Returns True if a button was clicked.
_SUBMIT_NEAR_COMPOSER_JS = (
    "let root=arguments[0]; for(let i=0;i<7 && root.parentElement;i++) root=root.parentElement;"
    "const b=[...root.querySelectorAll('button')].find(x=>!x.disabled && x.offsetParent!==null &&"
    "['comment','post','reply'].includes((x.innerText||'').trim().toLowerCase()) &&"
    "!['comment','reply'].includes((x.getAttribute('aria-label')||'').toLowerCase()));"
    "if(b){b.click(); return true;} return false;")


def _composer_submitted(driver, composer, text: str) -> bool:
    """True only if the text actually posted: the composer cleared (or detached), or the text now
    shows in the nearby comment list — NOT merely still sitting in a full composer (the old
    'text in body' check false-positived on that, so comments silently never posted)."""
    try:
        if (composer.text or "").strip() == "":
            return True
    except Exception:
        return True  # composer detached/re-rendered after posting
    try:
        return bool(driver.execute_script(
            "let r=arguments[0]; for(let i=0;i<9 && r.parentElement;i++) r=r.parentElement;"
            "const cl=r.querySelector(\"[data-testid*='-commentList']\");"
            "return cl ? cl.innerText.includes(arguments[1]) : false;", composer, text[:25]))
    except Exception:
        return False


def _scroll_into_center(driver, element) -> None:
    """Best-effort: park `element` in the MIDDLE of the viewport. Positioning is never fatal on its
    own, so a failure here is swallowed and left to the click that follows."""
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        time.sleep(random.uniform(0.3, 0.8))
    except Exception:
        pass  # a stale element or a rejected scroll is not a failure — the click below decides


def _focus_composer(driver, composer) -> None:
    """Click into a comment/reply composer, centered first.

    LinkedIn's global nav is STICKY, so whatever the previous action on the card left on screen can
    leave the composer pinned to the very top of the viewport — the nav's own <svg> then receives
    the click and Chrome raises ElementClickInterceptedException at y≈9 (issue #815). Centering is
    the actual fix; a JS click would also dodge the nav but would equally dodge a genuine modal or
    overlay, so the one retry re-centers and clicks for real and a second interception is allowed
    to raise (the caller names the step it died on)."""
    _scroll_into_center(driver, composer)
    try:
        composer.click()
    except ElementClickInterceptedException:
        _scroll_into_center(driver, composer)
        composer.click()


def post_comment_inline(driver, wait, card, comment_text: str, user_id: int = None) -> bool:
    """Open the card's inline comment composer, type the comment, and submit via the composer's
    own Comment/Post button (the SDUI composer has no <form>). Returns True only if the comment
    actually lands (composer clears / appears in the list), not just because text was typed.

    A failure names the STEP it died on: one `try` over the whole sequence reported every failure
    mode as the same 'Inline comment post failed' warning, which both hid the real cause and
    collapsed unrelated faults into one escalated issue. Step names carry no quotes or digits on
    purpose — the escalation dedup key masks both, so quoting them would re-merge the very keys
    this split exists to separate."""
    step = "prepare text"
    try:
        comment_text = _strip_non_bmp(comment_text)  # ChromeDriver send_keys throws on non-BMP emoji
        if not comment_text.strip():
            return False
        step = "open composer"
        if click_first(driver, wait, _COMMENT_ACTION_LOCATORS,
                       "Open comment composer", parent_element=card, required=False, user_id=user_id) is None:
            return False
        time.sleep(random.uniform(1.5, 3))
        step = "find composer"
        # Scoped to THIS card. The feed walk comments on several posts without reloading the page and
        # LinkedIn leaves each composer mounted after it submits, so a document-wide lookup returns
        # the FIRST visible role=textbox in DOM order — an earlier post's composer, scrolled off the
        # top, which is how the click landed under the sticky nav at y=9 (issue #876). Centering that
        # composer (#815) would not have fixed it, it would have typed the comment into the wrong
        # post. No document-wide fallback on purpose: no composer on this card means skip the post.
        composer = find_first(driver, wait,
                              [(By.CSS_SELECTOR, "div[role='textbox'][aria-label*='creating comment']"),
                               (By.CSS_SELECTOR, "div[role='textbox']")],
                              "Comment composer", visible_only=True, required=False,
                              parent_element=card, user_id=user_id)
        if composer is None:
            return False
        step = "focus composer"
        _focus_composer(driver, composer)
        step = "type comment"
        composer.send_keys(comment_text)
        time.sleep(random.uniform(1, 2))
        step = "submit composer"
        if not driver.execute_script(_SUBMIT_NEAR_COMPOSER_JS, composer):
            composer.send_keys(Keys.CONTROL, Keys.RETURN)  # fallback
        time.sleep(random.uniform(3, 5))
        step = "verify submit"
        return _composer_submitted(driver, composer, comment_text)
    except Exception as e:
        log_warning(f"Inline comment post failed at {step}", exc=e, action_type="comment", user_id=user_id)
        return False


# ── Comment + reaction locator chains (issue #816 live grounding, 2026-08-02) ──────────────────
#
# Every chain here is ORDERED most-stable-first and every entry is a DIFFERENT route to the same
# control, because LinkedIn rotates which attribute is canonical and commonly keeps several alive
# at once. A single locator does not fail loudly — it returns None, and a None card or a None
# trigger is indistinguishable from "there was nothing to act on".
#
# Counts from the live run (9 posts on the home feed) are recorded per entry so the next drift is
# diagnosable against a baseline instead of a docstring.
#
# XPath entries use `.//` so they stay scoped when passed `parent_element=card`; a leading `//`
# would silently search the whole document and return a neighbouring post's control.
_COMMENT_ACTION_LOCATORS = [
    (By.CSS_SELECTOR, "button[aria-label='Comment']"),            # live count: 0 (was canonical)
    (By.CSS_SELECTOR, "button[aria-label^='Comment on']"),
    (By.CSS_SELECTOR, "[data-testid*='comment-button']"),
    (By.XPATH, f".//button[{_X_LOWER_TEXT}='comment']"),          # live count: 1 per card TODAY
    (By.XPATH, f".//*[@role='button'][{_X_LOWER_TEXT}='comment']"),
]

# The way IN to the reaction fly-out. The state button doubles as the default-Like toggle (its
# text is literally "Like"), so one chain serves both roles.
# Live: `button[aria-label^='Reaction button state']` matched 8 of 9 cards — still the best anchor.
_REACTION_TRIGGER_LOCATORS = [
    (By.CSS_SELECTOR, "button[aria-label^='Reaction button state']"),   # live count: 8
    (By.XPATH, f".//button[contains({_X_LOWER_ARIA},'reaction button state')]"),
    (By.XPATH, f".//button[contains({_X_LOWER_ARIA},'reaction')]"),
    (By.CSS_SELECTOR, "button[aria-label='React Like']"),
    (By.CSS_SELECTOR, "button[aria-label^='React']"),
    (By.CSS_SELECTOR, "[data-testid*='reaction']"),
    (By.XPATH, f".//button[{_X_LOWER_TEXT}='like']"),                   # exact: never "2 likes"
]

# The explicit fly-out opener. Live count: ZERO — hovering the trigger opens the fly-out directly
# now. Kept as a trailing fallback (other surfaces and older renders still ship it), but its
# absence is the documented normal path, so a miss here must never warn.
_REACTION_OPENER_LOCATORS = [
    (By.CSS_SELECTOR, "button[aria-label='Open reactions menu']"),      # live count: 0
    (By.XPATH, f".//button[contains({_X_LOWER_ARIA},'reactions menu')]"),
    (By.XPATH, ".//button[@aria-haspopup]"),
]


def _reaction_option_locators(reaction: str) -> list:
    """Ordered routes to ONE reaction inside the open fly-out.

    Document-scoped on purpose: the fly-out renders outside the card subtree (confirmed live — the
    options were only reachable from `driver`, never from the card), so scoping these to the card
    finds nothing even when the menu is wide open."""
    low = (reaction or "like").strip().lower()
    return [
        (By.CSS_SELECTOR, f"button[aria-label='{reaction}']"),           # live: exact labels present
        (By.XPATH, f"//button[{_X_LOWER_ARIA}='{low}']"),
        (By.XPATH, f"//*[@role='button'][{_X_LOWER_ARIA}='{low}']"),
        (By.XPATH, f"//*[self::button or @role='menuitem' or @role='menuitemradio' or "
                   f"@role='option'][{_X_LOWER_TEXT}='{low}']"),
    ]


def react_to_post_inline(driver, wait, card, post_content: str = None, comment_text: str = None,
                         user_id: int = None) -> Optional[bool]:
    """Leave a single reaction on the card's post via the SDUI reaction fly-out.

    Three-valued on purpose: True = a reaction registered, False = we tried and failed, None = the
    post already carried our reaction (a no-op, not a failure). None is falsy, so callers that only
    care whether a reaction landed keep working unchanged.

    Every locator is an ORDERED CHAIN (`_REACTION_TRIGGER_LOCATORS`, `_REACTION_OPENER_LOCATORS`,
    `_reaction_option_locators`) rather than a single anchor: LinkedIn rotates which of
    aria-label / data-testid / visible text is canonical and keeps several alive at once, so one
    locator per control is a silent single point of failure. Grounded against a live session
    2026-08-02 — the per-entry live counts are recorded beside each chain.

    The 2026-08 shape: the 'Reaction button state: …' button IS the trigger and doubles as the
    default-Like toggle (its text is literally "Like"), hovering it opens the fly-out directly, and
    'Open reactions menu' no longer exists. The fly-out renders OUTSIDE the card subtree, so the
    option lookup is document-scoped. The reaction itself is picked by a fast AI call scoped to the
    post + our comment, which self-falls-back to random. Returns True only if a reaction registered
    (the toggle no longer reads 'no reaction')."""
    try:
        state = find_first(driver, wait, _REACTION_TRIGGER_LOCATORS,
                           "Reaction state", parent_element=card, required=False, visible_only=True,
                           user_id=user_id)
        if state is not None and "no reaction" not in (state.get_attribute("aria-label") or "").lower():
            return None  # already reacted on this post — a no-op, not a failure

        reaction = choose_post_reaction(post_content, comment_text)
        # One chain serves trigger AND toggle now, so there is no second lookup to fall back to.
        trigger = state
        if trigger is not None:
            try:
                ActionChains(driver).move_to_element(trigger).perform()
                time.sleep(random.uniform(0.6, 1.2))
            except Exception:
                pass
        # The explicit opener is GONE as of 2026-08 (live count: 0) — hovering the trigger is what
        # opens the fly-out. Kept as a trailing fallback for surfaces that still ship it, but its
        # absence is now the NORMAL path, so it must never warn: warning on the documented happy
        # path is exactly the expected-no-op the recurrence rule turns into a filed defect.
        # max_try=1: this control is KNOWN absent on the current SDUI (live count 0), and the
        # supervised end-to-end run showed it still burning a full retry round — "Open reactions
        # menu | not found | .....retrying" — on every single card, for a lookup we expect to miss.
        # That retry cost on a doomed lookup is precisely what made the broken flow expensive.
        opened = click_first(driver, wait, _REACTION_OPENER_LOCATORS,
                             "Open reactions menu", parent_element=card, required=False,
                             warn_on_miss=False, max_try=1, user_id=user_id)
        time.sleep(random.uniform(0.8, 1.6))
        # Document-scoped: the fly-out renders outside the card subtree (confirmed live — the
        # options were reachable only from `driver`). Try the chosen reaction, then plain Like.
        # No longer gated on `opened`: the hover alone can open the menu, so requiring the opener
        # would skip a fly-out that is sitting right there.
        picked = click_first(driver, wait, _reaction_option_locators(reaction),
                             f"React {reaction}", required=False, warn_on_miss=False,
                             user_id=user_id)
        if picked is None and reaction.strip().lower() != "like":
            picked = click_first(driver, wait, _reaction_option_locators("Like"),
                                 "React Like", required=False, warn_on_miss=False, user_id=user_id)
        if picked is None:
            # Fly-out didn't open or the specific reaction wasn't found — fall back to clicking the
            # primary toggle directly, which leaves a default Like. Better a Like than no reaction.
            if trigger is None:
                return False
            driver.execute_script("arguments[0].click();", trigger)
        time.sleep(random.uniform(0.8, 1.5))
        wait_for_ajax(driver)
        # Best-effort confirm: label flips away from 'no reaction'. If the toggle can't be re-read,
        # trust the click rather than false-negative — so the miss only means something when the
        # card HAD a readable Reaction-state button before the click. Cards that never exposed one
        # (the fly-out/React-toggle path) take the documented trust-the-click fallback, and warning
        # about that on every card escalated to ERROR and filed a defect for working behaviour
        # (issue #875). A control this card never carried is also not worth waiting out twice.
        expected = state is not None
        after = find_first(driver, wait, [(By.CSS_SELECTOR, "button[aria-label^='Reaction button state']")],
                           "Reaction state (post-click)", parent_element=card, required=False,
                           visible_only=True, warn_on_miss=expected,
                           max_try=MAX_WAIT_RETRY if expected else 1, user_id=user_id)
        if after is not None and "no reaction" in (after.get_attribute("aria-label") or "").lower():
            # The card's controls were readable and the click STILL didn't take — the one reaction
            # failure none of the selector misses above stand for, so it gets its single warning
            # here, where it is detected. The caller's blanket warning is DEBUG (issue #878).
            log_warning("Reaction did not register after clicking", user_id=user_id,
                        action_type="comment")
            return False
        myprint(f"Reacted '{reaction}' on post")
        return True
    except Exception as e:
        log_warning("Inline post reaction failed", exc=e, action_type="comment", user_id=user_id)
        return False


def post_matches_preferences(content: str, author: str, prefs: dict) -> bool:
    """Decide whether a post should be engaged given the user's targeting preferences.

    Exclude keywords/topics/authors always kill it (case-insensitive substring). If any
    include constraint is set, the post must match at least one: a literal include keyword
    or author, OR (only if literal misses) LLM topic relevance to include_topics. With no
    include constraints, engage everything not excluded.
    """
    if not prefs:
        return True
    text = (content or "").lower()
    auth = (author or "").lower()
    if any(str(k).lower() in text for k in (prefs.get("exclude_keywords") or []) + (prefs.get("exclude_topics") or []) if k):
        return False
    if any(str(a).lower() in auth for a in (prefs.get("exclude_authors") or []) if a):
        return False
    incl_kw = [k for k in (prefs.get("include_keywords") or []) if k]
    incl_auth = [a for a in (prefs.get("include_authors") or []) if a]
    incl_topics = [t for t in (prefs.get("include_topics") or []) if t]
    if not (incl_kw or incl_auth or incl_topics):
        return True
    if any(str(k).lower() in text for k in incl_kw):
        return True
    if any(str(a).lower() in auth for a in incl_auth):
        return True
    if incl_topics and post_is_relevant(content, incl_topics):
        return True
    return False


def _topic_gate_topics(prefs: dict) -> list:
    """The topics a candidate post is judged on-topic against: the user's focus_topics (what they
    want authority in), falling back to include_topics when no focus is set."""
    focus = [t for t in ((prefs or {}).get("focus_topics") or []) if t]
    if focus:
        return focus
    return [t for t in ((prefs or {}).get("include_topics") or []) if t]


def passes_topic_gate(content: str, prefs: dict) -> bool:
    """Hard on-topic gate (issue #616). Under 2026 Topic Authority ranking an off-topic comment
    actively damages distribution, so a post that isn't about the user's focus topics is NEVER
    commented on — not by the strict path and not by the empty-filter fallback, which is exactly
    how the 2026-07-25 funnel ended up commenting on AI-in-HR posts for a non-HR account.

    With no topics configured there is nothing to be off-topic against, so the gate is inert. A
    literal topic mention short-circuits the classifier; the classifier itself fails OPEN (a
    lem-simple hiccup must not silence all engagement)."""
    topics = _topic_gate_topics(prefs)
    if not topics:
        return True
    text = (content or "").lower()
    if any(_mentions_topic(text, t) for t in topics):
        return True
    return post_is_relevant(content, topics)


def _mentions_topic(text: str, topic: str) -> bool:
    """Whole-term match for the gate's literal short-circuit. Substring matching would let a short
    topic ("HR") fire on an unrelated word ("thrive") and skip the classifier entirely, so the term
    has to be bounded by non-word characters on both sides."""
    term = str(topic or "").strip().lower()
    if not term:
        return False
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) is not None


# Roster blend (issue #616): the mix every fast-growing native creator studied engaged on —
# ~50% peers, 30% ICP, 20% large creators. Buckets that run dry spill their slots to the others.
_ROSTER_MIX = (("peer", 0.5), ("icp", 0.3), ("creator", 0.2))
# Anti-pod: at most ONE comment per roster author per run, on top of their weekly cap. Repeatedly
# engaging the same account in a session is the pod signature LinkedIn demotes.
_ROSTER_MAX_POSTS_PER_AUTHOR = 1


def _target_staleness(target: dict) -> tuple:
    """Rotation sort key: never-engaged targets first, then least-recently-engaged."""
    last = target.get("last_engaged_at")
    if last is None:
        return (0, 0.0)
    try:
        return (1, last.timestamp())
    except AttributeError:
        return (1, 0.0)


def select_roster_targets(targets: list, limit: int) -> list:
    """Which roster authors to engage this run: active targets still under their per-author weekly
    cap, drawn in the 50/30/20 peer/ICP/creator blend, least-recently-engaged first."""
    if limit <= 0:
        return []
    eligible = []
    for t in targets or []:
        if not t.get("active", True):
            continue
        cap = int(t.get("max_comments_per_week") or ENGAGEMENT_TARGET_WEEKLY_DEFAULT)
        if int(t.get("comments_this_week") or 0) >= cap:
            continue
        eligible.append(t)
    buckets = {category: [] for category, _ in _ROSTER_MIX}
    for t in sorted(eligible, key=_target_staleness):
        buckets[t.get("category") if t.get("category") in buckets else "peer"].append(t)
    picked = []
    for category, share in _ROSTER_MIX:
        quota = int(limit * share)
        picked.extend(buckets[category][:quota])
        buckets[category] = buckets[category][quota:]
    # Rounding + short buckets leave slots over; fill them with the stalest targets left, whatever
    # their category, so a lopsided roster still uses the whole run budget.
    leftovers = sorted([t for b in buckets.values() for t in b], key=_target_staleness)
    picked.extend(leftovers[:max(0, limit - len(picked))])
    return picked[:limit]


def _roster_activity_url(profile_url: str) -> str:
    """A roster author's recent-activity page — the roster's equivalent of the home feed."""
    base = str(profile_url or "").strip().rstrip("/")
    if not base:
        return ""
    if "/recent-activity/" in base:
        return base + "/"
    return f"{base}/recent-activity/all/"


# What the run's feed sort actually was. Only FEED_SORT_RECENT means the recency-dominant scoring
# matrix (#622) ranked a recency-ordered feed; every other value means it ranked whatever LinkedIn's
# algorithm served, and the scan must not be read as if recency sorting was active (#817).
FEED_SORT_RECENT = "recent"            # confirmed on 'Recent' by the control itself
FEED_SORT_TOP = "top"                  # control found, still on the algorithmic sort
FEED_SORT_MISSING = "missing"          # no sort control on the home feed at all
FEED_SORT_UNKNOWN = "unknown"          # control there, but which sort applies could not be read
FEED_SORT_NOT_APPLICABLE = "n/a"       # a surface that never had one (group feed, roster activity)

# Ordered fallback chain for the home-feed sort trigger, most-stable anchor first: aria-label →
# data-testid → visible 'Sort by' text → a popup trigger whose whole label IS the current sort (the
# 'Sort by' prefix is dropped on narrow layouts) → any non-<button> role=button carrying 'sort'.
# Class names are never keyed on — SDUI churns them.
_FEED_SORT_LOCATORS = [
    (By.XPATH, f"//button[contains({_X_LOWER_ARIA},'sort')]"),
    (By.XPATH, f"//*[self::button or @role='button'][contains({_X_LOWER_TESTID},'sort')]"),
    (By.XPATH, f"//button[contains({_X_LOWER_TEXT},'sort by')]"),
    (By.XPATH, f"//button[@aria-haspopup][{_X_LOWER_TEXT}='{FEED_SORT_TOP}' or "
               f"{_X_LOWER_TEXT}='{FEED_SORT_RECENT}']"),
    (By.XPATH, f"//*[@role='button'][contains({_X_LOWER_ARIA},'sort')]"),
]

_FEED_RECENT_OPTION_LOCATORS = [
    (By.XPATH, "//*[self::button or self::li or @role='menuitem' or @role='menuitemradio' "
               f"or @role='option'][{_X_LOWER_TEXT}='{FEED_SORT_RECENT}']"),
    (By.XPATH, "//*[self::button or @role='menuitem' or @role='menuitemradio' or @role='option']"
               f"[contains({_X_LOWER_TEXT},'{FEED_SORT_RECENT}')]"),
    (By.XPATH, f"//*[{_X_LOWER_TEXT}='{FEED_SORT_RECENT}']"),
]


def _is_home_feed(driver) -> bool:
    """True only on linkedin.com/feed itself. Group feeds and a roster author's recent-activity page
    reuse the same commenting engine but never had a home-feed sort control, so a miss there is an
    expected no-op — warning on it would file a defect for working behaviour.

    An unreadable URL counts as NOT the home feed (issue #872): a dead session cannot say which
    surface it was on, and escalating on a guess costs a triage for working behaviour. A false
    silence loses one signal; a false defect costs a person."""
    try:
        path = str(driver.current_url or "").split("?")[0].split("#")[0].lower()
    except Exception:
        return False
    return path.rstrip("/").endswith("/feed")


def _feed_sort_state(control) -> str:
    """Which sort a found control reports — FEED_SORT_RECENT / FEED_SORT_TOP, or '' when its label
    is unreadable. '' is load-bearing: 'we could not tell' must never be recorded as 'recent'.

    A label naming BOTH sorts is unreadable too. Some dropdown triggers spell their options into
    the accessible name ('Sort by, currently Top, options Top and Recent'), and taking 'recent' from
    one would do the two worst things at once: skip the flip below (the label already 'says' Recent)
    and record the run as sorted — the exact lie #817 exists to stop."""
    if control is None:
        return ""
    try:
        label = f"{control.get_attribute('aria-label') or ''} {control.text or ''}".lower()
    except Exception:
        return ""
    has_recent, has_top = FEED_SORT_RECENT in label, FEED_SORT_TOP in label
    if has_recent and not has_top:
        return FEED_SORT_RECENT
    if has_top and not has_recent:
        return FEED_SORT_TOP
    return ""


def _switch_feed_to_recent(driver, wait, user_id: int = None) -> str:
    """Flip the home feed's sort from 'Top' to 'Recent' and REPORT what the run actually got.

    A silent no-op was fine before #622 made feed scoring recency-dominant; it is not now. With the
    sort control missing, `_score_feed_post`'s recency term ranks a candidate pool LinkedIn has
    already reordered by engagement, so the run quietly degrades to roughly what #622 replaced. The
    return value is the run's sort state and it rides into the feed funnel + `feed_scan` event, so a
    scan is never read as recency-sorted when it wasn't (#817).

    FEED_SORT_RECENT is returned ONLY when the control confirms it afterwards — an unverified flip
    recorded as sorted tells the same lie the silent no-op told. Fail-fast (`max_try=1`): this runs
    twice a run and each retry round burned MAX_WAIT_RETRY x ~5s before reporting the same miss.
    """
    try:
        if not _is_home_feed(driver):
            log_debug("Skipping feed sort — not the home feed", action_type="scrape", user_id=user_id)
            return FEED_SORT_NOT_APPLICABLE
        btn = find_first(driver, wait, _FEED_SORT_LOCATORS, "Feed sort control", required=False,
                         visible_only=True, max_try=1, user_id=user_id)
        if btn is None:
            return FEED_SORT_MISSING
        # The control reads 'Recent' once flipped — skip re-opening the menu so the second caller in
        # a run (navigate_to_feed then comment_on_feed_inline) is a cheap no-op.
        if _feed_sort_state(btn) == FEED_SORT_RECENT:
            return FEED_SORT_RECENT
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(random.uniform(1, 2))
        opt = find_first(driver, wait, _FEED_RECENT_OPTION_LOCATORS, "Recent sort option",
                         required=False, visible_only=True, max_try=1, user_id=user_id)
        if opt is None:
            return FEED_SORT_TOP
        driver.execute_script("arguments[0].click();", opt)
        time.sleep(random.uniform(2, 3.5))
        after = find_first(driver, wait, _FEED_SORT_LOCATORS, "Feed sort control", required=False,
                           visible_only=True, max_try=1, user_id=user_id)
        return _feed_sort_state(after) or FEED_SORT_UNKNOWN
    except Exception as e:
        log_warning("Feed recent-sort failed", exc=e, action_type="scrape", user_id=user_id)
        return FEED_SORT_UNKNOWN


_FEED_FUNNEL_KEY = "linkedin:feed_funnel:{user_id}"
_FEED_FUNNEL_TTL = 30 * 24 * 60 * 60  # keep the last scan's reach estimate for 30 days
# Consecutive top-candidate include-misses before we relax to the fallback (comment on the best feed
# post regardless of the include filters). Hard excludes / recency / min-reactions still apply.
_FEED_FALLBACK_AFTER_MISSES = 6


def set_feed_funnel(user_id: int, funnel: dict) -> None:
    """Persist the last feed scan's reach funnel (posts examined -> matched -> commented) so the UI
    can show the user how strict their targeting is. Best-effort; no-op without Redis."""
    client = _redis_client()
    if client is None:
        return
    try:
        client.set(_FEED_FUNNEL_KEY.format(user_id=user_id), json.dumps(funnel), ex=_FEED_FUNNEL_TTL)
    except Exception as e:
        log_warning("Could not store feed funnel", exc=e, user_id=user_id, action_type="comment")


def get_feed_funnel(user_id: int) -> "dict | None":
    """Last feed scan's reach funnel for a user, or None if there hasn't been one recently."""
    client = _redis_client()
    if client is None:
        return None
    try:
        raw = client.get(_FEED_FUNNEL_KEY.format(user_id=user_id))
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    except (ValueError, TypeError):
        return None


def _engage_card(driver, wait, my_profile: LinkedInProfile, user_id: int, card, key: str,
                 content: str, author: str, prefs: dict, profile_synthesis,
                 used_comment_shapes: list, recent_comments: list = None) -> bool:
    """Claim -> generate -> react -> comment on ONE post card. True only when the comment actually
    landed. Shared by the roster pass and the feed walk so both go through the same at-most-once
    claim, the same per-run blueprint rotation, and the same react-before-submit ordering.

    `recent_comments` is the user's own recent comment history (newest first) that the quality gate
    dedups this draft against; a comment that lands is prepended to it, so two posts in the SAME run
    can't get near-identical comments either (issue #617)."""
    if not claim_post_for_comment(user_id, key):
        return False
    comment_blueprint = select_blueprint("comment", recent_formats=used_comment_shapes)
    with llm_attribution(user_id=user_id, feature=FEATURE_COMMENT):
        comment_text = generate_ai_response(content, my_profile, None, prefs=prefs,
                                            profile_synthesis=profile_synthesis,
                                            blueprint=comment_blueprint,
                                            recent_comments=recent_comments, user_id=user_id)
    if comment_text and comment_blueprint.get("format"):
        used_comment_shapes.insert(0, comment_blueprint["format"])
    if not comment_text:
        release_post_claim(user_id, key)  # no comment generated (or none cleared the quality gate)
        return False
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", card)
    # Read-time-realistic delay (issue #626): scaled to the post's length and floored well above
    # "instant", so we never comment faster than a human could have read the thing. The old
    # half-reading-time + thinking-time pair could clear a short post in ~3s, which is the loudest
    # cadence tell there is.
    pace_read(content, user_id=user_id)
    # React BEFORE submitting the comment: posting re-renders the card and staled the element, so
    # the old post-comment reaction attempt silently failed. Skip our OWN posts. Non-fatal — a
    # missed reaction never blocks the comment.
    #
    # Gated OFF by default since #816. DEBUG, not a warning: a deliberate stand-down is working
    # behaviour, and warning it every card is exactly the "expected no-op" that escalates into a
    # filed defect. The commenting path below is untouched.
    if not INLINE_REACTIONS_ENABLED:
        log_debug("Inline reactions disabled (INLINE_REACTIONS_ENABLED=False, issue #816)",
                  user_id=user_id, action_type="comment")
    elif not _author_is_me(author, my_profile):
        outcome = react_to_post_inline(driver, wait, card, post_content=content,
                                       comment_text=comment_text, user_id=user_id)
        if outcome is None:
            # Already reacted is a no-op, not a failure. Reporting it as one made a benign skip
            # indistinguishable from a broken selector, and now that repeated warnings escalate it
            # would file a defect for working behaviour.
            log_debug("Post already carried our reaction — skipping", user_id=user_id,
                      action_type="comment")
        elif outcome:
            mark_post_reacted(user_id, key)
        else:
            # Every way react_to_post_inline reports a failure has already warned where it happened
            # — the fly-out opener's miss when there was no toggle to default-Like (issue #873), the
            # reaction click's own miss, the wrapped exception, or the click that never registered.
            # Warning again from out here filed a SECOND PostHog defect for one condition (#878).
            log_debug("No reaction landed on post — continuing to the comment", user_id=user_id,
                      action_type="comment")
    if not post_comment_inline(driver, wait, card, comment_text, user_id=user_id):
        release_post_claim(user_id, key)  # posting failed — let a later run retry
        return False
    mark_post_commented(user_id, key)
    insert_new_log(user_id=user_id, action_type=LogActionType.COMMENT,
                   result=LogResultType.SUCCESS, post_url=key, message=comment_text)
    record_action(user_id, ACTION_COMMENT)  # account-level governor (issue #626)
    if recent_comments is not None:
        recent_comments.insert(0, comment_text)
    time.sleep(random.uniform(6, 14))  # human pacing between comments
    return True


def comment_on_roster_posts(driver, wait, my_profile: LinkedInProfile, user_id: int, max_posts: int,
                            prefs: dict, profile_synthesis, used_comment_shapes: list, seen: set,
                            deadline_ts: float = None, recent_comments: list = None) -> dict:
    """Comment on the user's CURATED engagement roster before the home feed ever gets a look
    (issue #616): each selected target's recent-activity page is opened, and the first post that
    clears hard excludes, the dedup ledger and the on-topic gate gets a comment.

    Fail-closed by design — an author page that renders no commentable card (selector drift, an
    auth wall, a profile with only reshares) is logged and skipped, never guessed at. Returns the
    run counters the caller folds into the feed funnel."""
    stats = {"posted": 0, "targets_visited": 0, "examined": 0, "off_topic_skipped": 0,
             "key_sources": {}, "commented_key_sources": {}}
    if max_posts <= 0:
        return stats
    targets = get_engagement_targets(user_id, active_only=True)
    if not targets:
        return stats
    for target in select_roster_targets(targets, max_posts):
        if stats["posted"] >= max_posts or (deadline_ts and time.time() >= deadline_ts):
            break
        profile_url = target.get("profile_url")
        url = _roster_activity_url(profile_url)
        if not url:
            continue
        try:
            driver.get(url)
            wait_for_ajax(driver)
        except Exception as e:
            log_warning("Could not open roster target's activity page", exc=e, user_id=user_id,
                        action_type="comment", task_name="comment_on_roster_posts")
            continue
        time.sleep(random.uniform(2, 4))
        stats["targets_visited"] += 1
        weekly_left = max(0, int(target.get("max_comments_per_week") or ENGAGEMENT_TARGET_WEEKLY_DEFAULT)
                          - int(target.get("comments_this_week") or 0))
        allowed_here = min(_ROSTER_MAX_POSTS_PER_AUTHOR, weekly_left, max_posts - stats["posted"])
        posted_here = 0
        for box in driver.find_elements(By.CSS_SELECTOR, _FEED_POST_TEXT_SEL):
            if posted_here >= allowed_here or (deadline_ts and time.time() >= deadline_ts):
                break
            try:
                content = (box.text or "").strip()
            except StaleElementReferenceException:
                continue
            if len(content) < 20:
                continue
            card = _card_for_textbox(driver, box)
            if card is None:
                continue  # no comment affordance on this item — not a commentable post
            author = _post_author_from_card(card) or (target.get("name") or "")
            key, key_source = _feed_post_identity(card, author, content, driver=driver)
            fps = _feed_content_fingerprints(author, content)
            if key in seen or (fps & seen):
                continue
            stats["examined"] += 1
            stats["key_sources"][key_source] = stats["key_sources"].get(key_source, 0) + 1
            seen.add(key)
            seen.update(fps)
            if (has_commented_post(user_id, key) or has_user_commented_on_post_url(user_id, key)
                    or not _passes_hard_excludes(content, author, prefs)):
                continue
            if not passes_topic_gate(content, prefs):
                stats["off_topic_skipped"] += 1
                log_info(f"Skipped roster post by {author or profile_url}: off-topic for the "
                         f"user's focus topics", user_id=user_id, action_type="comment",
                         task_name="comment_on_roster_posts")
                continue
            if _engage_card(driver, wait, my_profile, user_id, card, key, content, author, prefs,
                            profile_synthesis, used_comment_shapes, recent_comments):
                posted_here += 1
                stats["posted"] += 1
                stats["commented_key_sources"][key_source] = \
                    stats["commented_key_sources"].get(key_source, 0) + 1
                record_target_engagement(user_id, profile_url)
                myprint(f"Commented on roster target {author or profile_url} "
                        f"({target.get('category')})")
        if posted_here == 0:
            log_info(f"No commentable on-topic post found for roster target {profile_url}",
                     user_id=user_id, action_type="comment", task_name="comment_on_roster_posts")
    return stats


def comment_on_feed_inline(driver, wait, my_profile: LinkedInProfile, user_id: int,
                           max_posts: int = 10, deadline_ts: float = None, prefs: dict = None,
                           engagers: set = None) -> int:
    """Comment on the user's curated engagement ROSTER first (issue #616), then walk the SDUI feed
    for whatever budget is left, prioritizing by a scoring matrix instead of DOM order:
    recency-dominant (golden hour), then relevance, reciprocity (people who engaged with us), and
    healthy activity. Applies targeting filters + per-day cap + a max-post-age gate, and never
    comments on a post that fails the on-topic gate. Returns the total number of comments posted."""
    from selenium.common.exceptions import StaleElementReferenceException
    if prefs is None:
        prefs = get_engagement_preferences(user_id)
    if engagers is None:
        engagers = get_recent_engagers(user_id)
    daily_cap = prefs.get("max_comments_per_day") or 20
    # Human pacing (issue #626): today's allowance is a stable random draw from the cap (with
    # weekend asymmetry and occasional rest days), and the account-level governor also caps the
    # COMBINED comment/DM/invite traffic — so a flat "cap comments every day" volume signature
    # never shows up, and the lanes can't each spend a full cap on the same day.
    remaining_today = remaining_actions(user_id, ACTION_COMMENT, daily_cap,
                                        count_comments_today(user_id),
                                        caps=engagement_caps_from_prefs(prefs))
    if remaining_today <= 0:
        myprint(f"Daily comment budget spent (cap {daily_cap}) — skipping")
        return 0
    max_posts = min(max_posts, remaining_today)
    max_age_min = (prefs.get("max_post_age_hours") or 24) * 60
    min_reactions = prefs.get("min_reactions") or 0
    # Stable VOICE synthesis (cached weekly, lazily created on first use) — the voice source for every
    # comment this run, in place of the bloated/volatile full profile JSON.
    profile_synthesis = get_or_create_profile_synthesis(user_id, my_profile)

    # Per-run comment ANGLE rotation from the shared framework core: each comment this run gets a
    # different archetype (Expander, Storyteller, Questioner, ...) so a day's comments never all
    # read from the same template. In-memory only — comments are too high-volume to justify a DB
    # shape history, and per-run rotation is what a reader of the same feed would notice.
    used_comment_shapes: list = []

    # The user's own recent comments — what the comment-side similarity gate dedups each fresh draft
    # against (issue #617). Loaded once per run and appended to as comments land, so neither a
    # rewording of yesterday's comment nor two near-identical comments in this run can ship.
    try:
        recent_comments: list = list(get_recent_comment_texts(user_id))
    except Exception as e:
        log_warning("Could not load recent comment history; similarity gate degrades to none",
                    exc=e, user_id=user_id, action_type="comment")
        recent_comments = []

    posted, seen, scrolls = 0, set(), 0
    # Roster FIRST (issue #616): curated peers / ICP / large creators outrank whatever the home
    # feed happens to serve. An empty roster returns zeros here and the run degrades to the plain
    # feed walk below. `seen` is shared, so a roster post can never be re-commented from the feed.
    roster_stats = comment_on_roster_posts(driver, wait, my_profile, user_id, max_posts, prefs,
                                           profile_synthesis, used_comment_shapes, seen,
                                           deadline_ts=deadline_ts, recent_comments=recent_comments)
    posted = roster_stats["posted"]
    off_topic_skipped = 0

    if roster_stats["targets_visited"]:
        navigate_to_feed(driver, wait)  # the roster pass navigated away from the feed
    # Surface golden-hour posts; scoring still ranks them. The returned state is recorded on the
    # funnel + the feed_scan event — an unsorted scan ranked an algorithmic candidate pool (#817).
    feed_sort = _switch_feed_to_recent(driver, wait, user_id=user_id)
    # Reach funnel (surfaced to the user so they can tell when their targeting is too strict) and the
    # empty-filter fallback. examined = posts we looked at; hard = passed excludes/recency/min-reactions;
    # include = also matched the user's include topics/keywords/authors.
    examined_keys, hard_keys, include_keys = set(), set(), set()
    # Which SOURCE produced each post's dedup key (permalink / card URN / content hash). Surfaced on
    # the funnel + logs so a live run proves feed comments key on the stable URN — a hash-keyed
    # comment is the duplicate-prone path that caused #474/#580.
    key_source_by_key: dict = {}
    posted_key_sources: dict = {}
    strict_misses, fallback_active, fallback_used = 0, False, False
    # Posts that cleared excludes + dedup but failed ONLY the recency/min-reactions gates. Tracked
    # apart from the permanent `seen` set so the empty-feed fallback can RECONSIDER them (relaxing
    # those two gates) when nothing clears the hard filters at all. Without this, a sparse or
    # low-reaction feed produces zero comments even with feed_fallback_when_empty on — because the
    # existing include-miss fallback only triggers on posts that first passed the hard gates.
    soft_seen: set = set()
    hard_relaxed = False
    _incl = [f for f in ((prefs.get("include_keywords") or []) + (prefs.get("include_authors") or [])
                         + (prefs.get("include_topics") or [])) if f]
    fallback_enabled = bool(prefs.get("feed_fallback_when_empty", True)) and bool(_incl)
    while posted < max_posts and scrolls < 15:
        if deadline_ts and time.time() >= deadline_ts:
            break
        # Gather + score every fresh candidate currently in view (cheap, no-LLM gates).
        candidates = []
        for box in driver.find_elements(By.CSS_SELECTOR, _FEED_POST_TEXT_SEL):
            try:
                content = (box.text or "").strip()
            except StaleElementReferenceException:
                continue
            if len(content) < 20:
                continue
            card = _card_for_textbox(driver, box)
            if card is None:
                continue
            author = _post_author_from_card(card)
            # Canonical URN-based key (stable across re-renders); normalized content hash only
            # when no URN exists. `fps` are render-stable fingerprints at several prefix lengths, so
            # even the URN-less fallback path can't slip a second comment through when a re-render
            # truncates the body differently (#474, recurred as #580).
            key, key_source = _feed_post_identity(card, author, content, driver=driver)
            fps = _feed_content_fingerprints(author, content)
            if key in seen or (fps & seen):
                continue
            key_source_by_key[key] = key_source
            examined_keys.add(key)
            # Persistent, cross-run/worker dedup: skip anything already claimed or commented
            # (commented_posts ledger), plus historical SUCCESS comment logs, plus hard excludes.
            if (has_commented_post(user_id, key) or has_user_commented_on_post_url(user_id, key)
                    or not _passes_hard_excludes(content, author, prefs)):
                seen.add(key)
                seen.update(fps)
                continue
            # Recency + min-reactions are soft gates: when the whole feed fails them the empty-feed
            # fallback relaxes them (below), so a reject here goes to soft_seen (reconsiderable),
            # not the permanent `seen`. Skip already-soft-rejected posts until we relax.
            if not hard_relaxed and key in soft_seen:
                continue
            age = _post_age_minutes(driver, card)
            counts = _post_social_counts(card)
            if not hard_relaxed:
                if age is not None and age > max_age_min:        # recency gate
                    soft_seen.add(key)
                    continue
                if min_reactions and counts["reactions"] < min_reactions:
                    soft_seen.add(key)
                    continue
            meta = {"author": author, "age_minutes": age, "comments": counts["comments"],
                    "reactions": counts["reactions"], "relevant": _literal_relevant(content, author, prefs)}
            hard_keys.add(key)
            candidates.append((_score_feed_post(meta, prefs, engagers), key, card, content, author, age,
                               fps, key_source))

        if candidates:
            candidates.sort(key=lambda c: c[0], reverse=True)
            score, key, card, content, author, age, fps, key_source = candidates[0]
            seen.add(key)        # decided on this one either way
            seen.update(fps)     # and its render-stable fingerprints (URN-less fallback guard)
            # Include gate (may use the LLM topic classifier) on the chosen post. If the feed keeps
            # producing nothing that matches the user's include filters, RELAX to fallback for the rest
            # of the run — comment on the best feed post regardless of include (LinkedIn already curates
            # the feed to relevant content). Hard excludes / recency / min-reactions still applied.
            if not fallback_active and fallback_enabled and strict_misses >= _FEED_FALLBACK_AFTER_MISSES:
                fallback_active = True
                myprint("Feed targeting matched nothing — falling back to top feed posts for this run")
            # On-topic gate FIRST and unconditionally (issue #616): the fallback may widen WHICH
            # posts qualify, but it may never put a comment on a post that is off-topic for the
            # user's focus topics — that is what damaged distribution in the 2026-07-25 funnel.
            if not passes_topic_gate(content, prefs):
                off_topic_skipped += 1
                log_info(f"Skipped feed post by {author or 'unknown author'}: off-topic for the "
                         f"user's focus topics", user_id=user_id, action_type="comment",
                         task_name="comment_on_feed_inline")
                continue
            if fallback_active:
                fallback_used = True
            elif post_matches_preferences(content, author, prefs):
                include_keys.add(key)
            else:
                strict_misses += 1
                continue
            # _engage_card atomically claims the post BEFORE spending an LLM call or commenting. If
            # a prior/concurrent run already holds it, we lose the race there and move on — at most
            # one comment per post per user, across the pre-post run, the golden-hour run, and retries.
            if _engage_card(driver, wait, my_profile, user_id, card, key, content, author, prefs,
                            profile_synthesis, used_comment_shapes, recent_comments):
                posted_key_sources[key_source] = posted_key_sources.get(key_source, 0) + 1
                log_info(f"Feed comment keyed by {key_source} ({key})", user_id=user_id,
                         action_type="comment", task_name="comment_on_feed_inline")
                posted += 1
                myprint(f"Commented on {author or 'a'}'s post "
                        f"(score {score:.2f}, age {'?' if age is None else str(age) + 'm'}) ({posted}/{max_posts})")
            continue  # DOM re-rendered / candidate consumed — re-gather from the top
        # Nothing cleared the hard filters this pass. If the whole feed keeps coming up empty
        # (0 posts past excludes + recency + min-reactions) but some only missed the recency/
        # min-reactions gates, relax THOSE gates once and re-scan from the top — otherwise a
        # sparse or low-reaction feed yields zero comments even with feed_fallback_when_empty on
        # (excludes/dedup still apply; we still comment on the best-scored post).
        if (fallback_enabled and not hard_relaxed and posted == 0 and not hard_keys
                and soft_seen and scrolls + 1 >= _FEED_FALLBACK_AFTER_MISSES):
            hard_relaxed = True
            fallback_active = True
            myprint("No posts cleared the recency/min-reaction gates — relaxing them "
                    "(empty-feed fallback) and re-scanning the top of the feed")
            driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(random.uniform(2.0, 3.5))
            continue
        # nothing actionable in view — scroll to load more
        driver.execute_script("window.scrollBy(0, 1200);")
        scrolls += 1
        time.sleep(random.uniform(2.5, 4))

    examined_key_sources: dict = {}
    for source in key_source_by_key.values():
        examined_key_sources[source] = examined_key_sources.get(source, 0) + 1
    for source, count in roster_stats["key_sources"].items():
        examined_key_sources[source] = examined_key_sources.get(source, 0) + count
    for source, count in roster_stats["commented_key_sources"].items():
        posted_key_sources[source] = posted_key_sources.get(source, 0) + count
    feed_commented = posted - roster_stats["posted"]
    off_topic_total = off_topic_skipped + roster_stats["off_topic_skipped"]
    funnel = {
        "examined": len(examined_keys) + roster_stats["examined"],
        "passed_filters": len(hard_keys),      # cleared excludes + recency + min-reactions
        "matched_topics": len(include_keys),   # also matched include topics/keywords/authors
        "commented": posted,
        # Roster-sourced vs feed-sourced split (issue #616): a healthy account comments mostly on
        # its curated roster, so this is the number that says whether the roster is actually working.
        "roster_commented": roster_stats["posted"],
        "feed_commented": feed_commented,
        "roster_targets_visited": roster_stats["targets_visited"],
        "roster_examined": roster_stats["examined"],
        "off_topic_skipped": off_topic_total,  # failed the on-topic gate — never commented on
        "fallback_used": fallback_used,
        "key_sources": examined_key_sources,           # every post we looked at
        "commented_key_sources": posted_key_sources,   # only the ones we commented on
        "max_post_age_hours": prefs.get("max_post_age_hours") or 24,
        "min_reactions": min_reactions,
        # Which feed ordering the recency-dominant scoring matrix actually ranked (#817). Anything
        # other than FEED_SORT_RECENT means the candidate pool was LinkedIn's algorithmic one.
        "feed_sort": feed_sort,
        "at": datetime.now().isoformat(),
    }
    set_feed_funnel(user_id, funnel)
    track_feed_scan(user_id, funnel)
    log_info(f"Engagement scan: examined {len(examined_keys) + roster_stats['examined']}, "
             f"passed filters {len(hard_keys)}, matched topics {len(include_keys)}, "
             f"commented {posted} (roster {roster_stats['posted']} / feed {feed_commented}), "
             f"off-topic skipped {off_topic_total}, fallback={fallback_used}, "
             f"sort {feed_sort}, key sources {examined_key_sources}", user_id=user_id,
             action_type="comment", task_name="comment_on_feed_inline")
    if posted_key_sources.get("hash"):
        log_warning(f"{posted_key_sources['hash']} of {posted} feed comments used an unstable "
                    f"content-hash key — no activity URN on those cards (duplicate risk, #580)",
                    user_id=user_id, action_type="comment", task_name="comment_on_feed_inline")
    return posted


# How far ABOVE a comment's own top edge a composer may still start and count as its reply box.
# Only absorbs sub-pixel/rounding drift — a real reply box opens below the comment, never above it.
_COMPOSER_ABOVE_SLACK_PX = 8


def _visible_rect(element: WebElement) -> dict | None:
    """Page-coordinate rect of a RENDERED element, else None. Zero-size IS the hidden case here —
    the same width>0 && height>0 test #478 applies to composer candidates."""
    try:
        r = element.rect or {}
    except Exception:
        return None  # stale/detached element is not a candidate
    if not r.get("width") or not r.get("height"):
        return None
    return r


def _visible_composers(root: WebDriver | WebElement) -> list[tuple[WebElement, dict]]:
    """`(element, rect)` for every rendered role=textbox under `root` — a WebElement to search one
    comment's subtree, or the driver to search the page."""
    found = []
    try:
        for box in root.find_elements(By.CSS_SELECTOR, "div[role='textbox']"):
            rect = _visible_rect(box)
            if rect:
                found.append((box, rect))
    except Exception:
        pass  # a stale root has no candidates; the caller skips
    return found


def _in_same_comment(driver: WebDriver, comment_el: WebElement, other: WebElement | None) -> bool:
    """True when `other` is this comment or shares its subtree (a reply wrapper inside it, or a
    wrapper holding it) — i.e. the composer that resolved to it is ours to type into."""
    if other is None:
        return False
    if other == comment_el:
        return True
    try:
        return bool(driver.execute_script(
            "return arguments[0].contains(arguments[1]) || arguments[1].contains(arguments[0]);",
            comment_el, other))
    except Exception:
        return False


def _reply_composer_for_comment(driver: WebDriver, comment_el: WebElement,
                                user_id: int = None) -> WebElement | None:
    """The reply composer belonging to THIS comment — never a page-wide first match.

    A document-wide role=textbox lookup returns the first VISIBLE composer in DOM order, so the reply
    was typed into the post's main 'Add a comment' box (it posts as a standalone comment) or into one
    left mounted by a comment replied to earlier in the same sweep. Same bug class as #478 on the
    other reply path and #876 on the post card; this is issue #883.

    Two rules, in order. A composer inside the comment's own subtree is unambiguous — LinkedIn nests
    a comment's replies, and the box that opens at the end of them, in the comment container. If this
    render puts it outside, fall back to #478's geometry: the visible composer NEAREST the comment's
    bottom edge, with anything above the comment rejected OUTRIGHT — that hard above-filter is what
    keeps the post's main box out, where #478 merely penalises it and still hands it back when it is
    the only candidate — and with a box that resolves to a DIFFERENT comment rejected too. No
    candidate means skip; we never borrow a composer.
    """
    anchor = _visible_rect(comment_el)
    if anchor is None:
        # The callers now rely on THIS function to log every miss (#886 dropped their own warning),
        # so a stale/unrendered comment must not return None silently.
        log_debug("Comment is not rendered; no reply composer to resolve",
                  action_type="reply", user_id=user_id)
        return None
    bottom = anchor["y"] + anchor["height"]
    nested = _visible_composers(comment_el)
    candidates = nested or [(box, rect) for box, rect in _visible_composers(driver)
                            if rect["y"] >= anchor["y"] - _COMPOSER_ABOVE_SLACK_PX]
    best = min(candidates, key=lambda br: abs(br[1]["y"] - bottom), default=None)
    if best is None:
        log_debug("No reply composer belongs to this comment", action_type="reply", user_id=user_id)
        return None
    if nested:
        return best[0]
    # Sibling render: reject a box that resolves to a DIFFERENT comment — the nearest box below can
    # belong to a LATER comment when our own reply box never opened, and borrowing it answers the
    # wrong person. An UNRESOLVED owner is not proof of that: `_comment_container` was written for a
    # comment BODY (`expandable-text-box`) and rejects any ancestor holding a GIF/Emoji composer
    # button, which is the composer's OWN toolbar here — requiring it to resolve would make this
    # branch skip every time, silently, whenever LinkedIn renders the reply box outside the comment.
    # Unresolved therefore falls through to #478's proven geometry, still under the hard above-filter
    # that is what actually keeps the post's main comment box out.
    owner = _comment_container(driver, best[0])
    if owner is not None and not _in_same_comment(driver, comment_el, owner):
        log_debug("Nearest reply composer belongs to another comment", action_type="reply", user_id=user_id)
        return None
    return best[0]


def _type_and_submit_reply(driver: WebDriver, composer: WebElement, reply_text: str,
                           user_id: int = None) -> bool:
    """Type into an ALREADY-resolved composer and submit (role=textbox + Ctrl+Enter fallback). Both
    reply paths share this so the submit/verify contract can never drift between them. True only when
    `_composer_submitted` confirms the post."""
    reply_text = _strip_non_bmp(reply_text)
    if not reply_text.strip():
        return False
    _focus_composer(driver, composer)  # sticky nav steals a top-of-viewport click (#815)
    composer.send_keys(reply_text)
    time.sleep(random.uniform(1, 2))
    if not driver.execute_script(_SUBMIT_NEAR_COMPOSER_JS, composer):
        composer.send_keys(Keys.CONTROL, Keys.RETURN)  # fallback
    time.sleep(random.uniform(3, 5))
    return _composer_submitted(driver, composer, reply_text)


def _reply_to_comment_inline(driver, wait, comment_el, reply_text: str, user_id: int = None) -> bool:
    """Open a comment's inline reply box, type the reply, and submit (same SDUI pattern as
    post_comment_inline: role=textbox composer + Ctrl+Enter fallback). The composer is resolved
    against THIS comment (`_reply_composer_for_comment`), never page-wide. Returns True if posted."""
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


def _comment_items_from_thread(driver):
    """Comment items on the SDUI thread — walk up from each Reply button to the container that
    also holds the author link + text (comments are no longer <article> elements)."""
    items = []
    reply_btns = find_all_first(driver, [
        (By.CSS_SELECTOR, "[data-testid*='-commentList'] button[aria-label='Reply']"),
        (By.CSS_SELECTOR, "button[aria-label='Reply']")])
    for rb in reply_btns:
        item = driver.execute_script(
            "let el=arguments[0],d=0;while(el&&d<8){"
            "if(el.querySelector&&el.querySelector(\"a[href*='/in/']\"))return el;"
            "el=el.parentElement;d++;}return arguments[0].parentElement;", rb)
        if item is not None:
            items.append(item)
    return items


def _fill_edition_description(driver, wait, subtitle: str) -> bool:
    """Best-effort: fill the newsletter edition-description field in the publish dialog. LinkedIn
    surfaces a 'what this edition is about' textarea/contenteditable whose placeholder/aria mentions
    'edition', 'about', or 'what this'. Non-fatal — the field wasn't present in one live run, so we
    never block publishing on it (fail fast: max_try=1, no retries)."""
    if not subtitle:
        return False
    candidates = []
    for attr in ("@placeholder", "@aria-label"):
        _l = f"translate({attr},'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')"
        for kw in ("edition", "about", "what this"):
            candidates.append(
                (By.XPATH, f"//*[(self::textarea or @role='textbox' or @contenteditable='true') "
                           f"and contains({_l},'{kw}')]"))
    try:
        desc_el = find_first(driver, wait, candidates, "Edition description",
                             visible_only=True, required=False, max_try=1)
        if desc_el is None:
            return False
        desc_el.click()
        desc_el.send_keys(_strip_non_bmp(subtitle))
        time.sleep(random.uniform(1, 2))
        return True
    except Exception:
        return False


def _fill_and_publish_article(driver, wait, title: str, body: str, subtitle: str = None,
                              user_id: "int | None" = None) -> "tuple[str | None, str | None]":
    """Fill LinkedIn's article editor (title textarea + contenteditable body) and run the
    Next → Publish flow. On the publish dialog, best-effort fills the edition description with
    `subtitle`.

    Uses the selector ladder in `cqc_lem.utilities.linkedin.article_editor` so a rotated entry point
    is caught by a fallback route and `failed_step` names the exact missing action. Returns
    `(published_url, None)` on success, or `(None, failed_step)` on failure.
    """
    return fill_article_editor(
        driver, wait, _strip_non_bmp(title), _strip_non_bmp(body),
        user_id=user_id,
        subtitle=subtitle,
        fill_description_fn=_fill_edition_description,
    )


def _tagged_edition_body(body: str, edition_id: "int | None" = None) -> str:
    """The edition body with any OWNED link in it UTM-tagged under this edition's campaign (#658).

    The generator is told not to put links in an edition at all (off-platform links suppress an
    article's reach), so on the mainline path this is a no-op — but the SPA lets the author edit a
    draft before it publishes, and a hand-added link to their own site is exactly the traffic worth
    attributing to the edition that sent it. Publish time is the right choke point: it is the last
    moment the body is still ours, and both publish tasks pass through here."""
    from cqc_lem.utilities.marketing.attribution import (MEDIUM_NEWSLETTER, SOURCE_NEWSLETTER,
                                                         campaign_for_edition, tag_links_in_text)
    return tag_links_in_text(body, SOURCE_NEWSLETTER, MEDIUM_NEWSLETTER,
                             campaign_for_edition(edition_id))


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_content')
def auto_publish_newsletter_edition(self, user_id: int):
    """Generate and publish a newsletter edition for the user (opt-in via newsletter_settings).
    Best-effort — the article publish flow is multi-step; the first real publish should be
    supervised. Repurposes the user's blog when align_with_blog is set."""
    settings = get_newsletter_settings(user_id)
    if not settings.get("enabled"):
        return "Newsletter not enabled"
    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Newsletter")
    except Exception as e:
        log_error("Error getting profile for newsletter", exc=e, user_id=user_id, task_name="auto_publish_newsletter_edition")
        return f"Failed to start newsletter: {e}"
    try:
        # TODO(blog-align): when align_with_blog, fetch recent blog/sitemap content to pass as
        # blog_content. For now the edition is generated from topic + profile.
        edition = generate_newsletter_edition(my_profile, topic=settings.get("topic"), blog_content=None)
        if not edition:
            return "No newsletter edition generated"
        driver.get("https://www.linkedin.com/article/new/")
        time.sleep(random.uniform(6, 9))
        url, failed_step = _fill_and_publish_article(driver, wait, edition["title"],
                                                      _tagged_edition_body(edition["body"]),
                                                      subtitle=edition.get("subtitle"),
                                                      user_id=user_id)
        if url:
            mark_newsletter_published(user_id, url)
            myprint(f"Published newsletter edition for user {user_id}: {edition['title']}")
            return f"Published newsletter: {edition['title']}"
        log_error("Newsletter publish flow did not complete",
                  user_id=user_id, task_name="auto_publish_newsletter_edition", failed_step=failed_step)
        return "Newsletter publish flow did not complete"
    except Exception as e:
        log_error("Newsletter publish error", exc=e, user_id=user_id, task_name="auto_publish_newsletter_edition")
        return f"Newsletter error: {e}"
    finally:
        quit_gracefully(driver)


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['edition_id']},
                  queue='se_content')
def auto_publish_edition(self, edition_id: int):
    """Publish a reviewed/untouched newsletter edition at its scheduled slot. Loads the pre-generated
    edition (draft or approved), fills LinkedIn's article editor, and records the outcome. Best-effort
    — the multi-step publish flow varies; first real publish should be supervised."""
    edition = get_newsletter_edition(edition_id)
    if not edition or edition.get("status") not in ("draft", "approved"):
        return f"Edition {edition_id} not publishable"
    user_id = edition["user_id"]
    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Newsletter")
    except Exception as e:
        log_error("Error getting profile for newsletter edition", exc=e, user_id=user_id, task_name="auto_publish_edition")
        return f"Failed to start newsletter edition: {e}"
    try:
        driver.get("https://www.linkedin.com/article/new/")
        time.sleep(random.uniform(6, 9))
        url, failed_step = _fill_and_publish_article(driver, wait, edition["title"],
                                                      _tagged_edition_body(edition["body"], edition_id),
                                                      subtitle=edition.get("subtitle"),
                                                      user_id=user_id)
        if url:
            mark_edition_published(edition_id, url)
            myprint(f"Published newsletter edition {edition_id} for user {user_id}: {edition['title']}")
            return f"Published newsletter edition: {edition['title']}"
        log_error("Newsletter edition publish flow did not complete",
                  user_id=user_id, task_name="auto_publish_edition", edition_id=edition_id, failed_step=failed_step)
        mark_edition_failed(edition_id)
        return "Newsletter edition publish flow did not complete"
    except Exception as e:
        log_error("Newsletter edition publish error", exc=e, user_id=user_id, task_name="auto_publish_edition")
        mark_edition_failed(edition_id)
        return f"Newsletter edition error: {e}"
    finally:
        quit_gracefully(driver)


def _parse_subscriber_count(text: "str | None") -> "int | None":
    """Pull a subscriber count out of LinkedIn's label text, e.g. '1,234 subscribers' -> 1234,
    '3.2K subscribers' -> 3200. Returns None when no count is present."""
    if not text:
        return None
    m = re.search(r"([\d.,]+)\s*([KkMm]?)\s*subscriber", text)
    if not m:
        return None
    num, suffix = m.group(1), m.group(2).upper()
    try:
        value = float(num.replace(",", ""))
    except ValueError:
        return None
    if suffix == "K":
        value *= 1_000
    elif suffix == "M":
        value *= 1_000_000
    return int(value)


def _read_newsletter_subscriber_count(driver, wait, newsletter_url: str) -> "int | None":
    """Best-effort: open the user's newsletter page and read its 'N subscribers' label. LinkedIn
    renders the count in a header near the newsletter title; selectors vary, so we scan a few
    candidates and fall back to a page-text regex. Returns None when the count can't be read —
    never raises (validated on a supervised first real run)."""
    if not newsletter_url:
        return None
    driver.get(newsletter_url)
    time.sleep(random.uniform(4, 7))
    el = find_first(driver, wait,
                    [(By.XPATH, "//*[contains(translate(text(),'SUBSCRIBER','subscriber'),'subscriber')]")],
                    "Subscriber count", visible_only=True, required=False, max_try=1)
    if el is not None:
        count = _parse_subscriber_count(getText(el) or "")
        if count is not None:
            return count
    try:
        return _parse_subscriber_count(driver.find_element(By.TAG_NAME, "body").text)
    except Exception:
        return None


def _invite_connections_to_newsletter(driver, wait, cap: int) -> int:
    """Best-effort: from the open newsletter page, invite up to `cap` connections to subscribe.
    Opens the 'Invite' dialog (LinkedIn labels it 'Invite connections'/'Manage'), selects up to
    `cap` connection checkboxes, and sends. Returns the number invited (0 when the flow isn't
    available or cap<=0). Never raises — caps are enforced here, opt-in is enforced by the caller
    (validated on a supervised first real run)."""
    if cap <= 0:
        return 0
    if click_first(driver, wait,
                   [(By.XPATH, "//button[contains(translate(@aria-label,'INVITE','invite'),'invite')]"),
                    (By.XPATH, "//button[contains(translate(normalize-space(),'INVITE','invite'),'invite')]")],
                   "Invite connections", required=False, max_try=1) is None:
        return 0
    time.sleep(random.uniform(2, 4))
    checkboxes = get_elements_as_list_wait_stale(
        wait, "//input[@type='checkbox' and contains(@id,'invitee')]",
        "Newsletter invitee checkboxes", max_retry=0) or []
    selected = 0
    for cb in checkboxes:
        if selected >= cap:
            break
        try:
            driver.execute_script("arguments[0].click();", cb)
            selected += 1
            time.sleep(random.uniform(0.2, 0.6))
        except Exception:
            continue
    if selected == 0:
        return 0
    if click_first(driver, wait,
                   [(By.XPATH, "//button[contains(translate(normalize-space(),'INVITE','invite'),'invite') "
                               "and not(@disabled)]")],
                   "Send newsletter invites", required=False, max_try=1) is None:
        return 0
    time.sleep(random.uniform(2, 4))
    return selected


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_content')
def track_newsletter_subscribers(self, user_id: int):
    """Capture the user's newsletter subscriber count over time and, when opted in, invite
    connections to subscribe within the per-run cap (issue #400). Reads the count from the
    newsletter page and records a growth snapshot; inviting only runs when
    invite_connections_enabled is set and stops at max_invites_per_run. Best-effort Selenium —
    the first real run should be supervised."""
    settings = get_newsletter_settings(user_id)
    if not settings.get("enabled"):
        return "Newsletter not enabled"
    newsletter_url = settings.get("newsletter_url")
    if not newsletter_url:
        return "No newsletter URL yet"
    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Newsletter")
    except Exception as e:
        log_error("Error getting profile for newsletter tracking", exc=e, user_id=user_id,
                  task_name="track_newsletter_subscribers")
        return f"Failed to start newsletter tracking: {e}"
    try:
        count = _read_newsletter_subscriber_count(driver, wait, newsletter_url)
        invited = 0
        if settings.get("invite_connections_enabled"):
            invited = _invite_connections_to_newsletter(driver, wait, int(settings.get("max_invites_per_run") or 0))
        record_newsletter_subscriber_stat(user_id, subscriber_count=count, invites_sent=invited)
        log_info("Newsletter subscriber snapshot", user_id=user_id,
                 task_name="track_newsletter_subscribers")
        return f"Subscribers: {count if count is not None else 'unknown'}; invited {invited}"
    except Exception as e:
        log_error("Newsletter subscriber tracking error", exc=e, user_id=user_id,
                  task_name="track_newsletter_subscribers")
        return f"Newsletter tracking error: {e}"
    finally:
        quit_gracefully(driver)


# JS: count the overflow ("…") buttons for OUR OWN comments on the current post. LinkedIn labels each
# comment's overflow control "View more options for <name>'s comment.", so matching our full name
# restricts this to comments WE authored — we never touch anyone else's comment.
_COUNT_OWN_COMMENT_MENUS_JS = (
    "const name=(arguments[0]||'').toLowerCase();"
    "return [...document.querySelectorAll('button[aria-label]')].filter(x=>{"
    "const a=(x.getAttribute('aria-label')||'').toLowerCase();"
    "return a.includes('options for') && a.includes('comment') && name && a.includes(name);"
    "}).length;")

# JS: open the overflow menu of the LAST of our own comments (we keep the first/earliest and delete
# the rest). The control is hover-hidden, so we JS-click it directly.
_OPEN_LAST_OWN_COMMENT_MENU_JS = (
    "const name=(arguments[0]||'').toLowerCase();"
    "const b=[...document.querySelectorAll('button[aria-label]')].filter(x=>{"
    "const a=(x.getAttribute('aria-label')||'').toLowerCase();"
    "return a.includes('options for') && a.includes('comment') && name && a.includes(name);"
    "});"
    "if(!b.length) return false;"
    "const t=b[b.length-1]; t.scrollIntoView({block:'center'}); t.click(); return true;")

# JS: click the "Delete" item in the open overflow menu.
_CLICK_DELETE_MENUITEM_JS = (
    "const el=[...document.querySelectorAll('[role=menuitem],[role=menuitemradio],button,div,span,li,h5')]"
    ".find(e=>/^delete\\b/i.test((e.innerText||'').trim()) && (e.innerText||'').trim().length<20);"
    "if(el){(el.closest('[role=menuitem]')||el).click(); return true;} return false;")

# JS: confirm deletion in the modal that follows (its confirm button reads exactly "Delete").
_CONFIRM_DELETE_MODAL_JS = (
    "const d=[...document.querySelectorAll('[role=dialog],.artdeco-modal')];"
    "for(const m of d){const btn=[...m.querySelectorAll('button')]"
    ".find(b=>((b.innerText||'').trim().toLowerCase()==='delete'));"
    "if(btn){btn.click(); return true;}} return false;")


def _delete_extra_own_comments_on_post(driver, my_full_name: str, dry_run: bool = True) -> Tuple[int, int]:
    """On the CURRENTLY-OPEN post page, keep our earliest comment and delete the rest so the post ends
    with exactly ONE comment from us. Returns (own_comments_found, deleted). In dry_run mode it only
    counts what WOULD be deleted (deleted stays 0). Only comments authored by `my_full_name` are ever
    touched — replies/comments by others are never affected."""
    try:
        found = int(driver.execute_script(_COUNT_OWN_COMMENT_MENUS_JS, my_full_name) or 0)
    except Exception as e:
        log_warning("Could not count own comments on post", exc=e, action_type="comment")
        return 0, 0
    if found <= 1:
        return found, 0
    if dry_run:
        return found, 0

    deleted = 0
    # Delete the last-of-ours repeatedly, re-counting each pass (the DOM re-renders after a delete).
    # Cap the loop at the initial surplus so a stuck menu can't spin forever.
    for _ in range(found - 1):
        try:
            if not driver.execute_script(_OPEN_LAST_OWN_COMMENT_MENU_JS, my_full_name):
                break
            time.sleep(random.uniform(1, 2))
            if not driver.execute_script(_CLICK_DELETE_MENUITEM_JS):
                log_warning("Delete menu item not found; stopping this post", action_type="comment")
                break
            time.sleep(random.uniform(1, 2))
            if not driver.execute_script(_CONFIRM_DELETE_MODAL_JS):
                log_warning("Delete confirm button not found; stopping this post", action_type="comment")
                break
            wait_for_ajax(driver)
            time.sleep(random.uniform(2, 3))
            deleted += 1
            remaining = int(driver.execute_script(_COUNT_OWN_COMMENT_MENUS_JS, my_full_name) or 0)
            if remaining <= 1:
                break
        except Exception as e:
            log_warning("Error deleting a duplicate comment", exc=e, action_type="comment")
            break
    return found, deleted


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_engage')
def consolidate_duplicate_comments_for_user(self, user_id: int, dry_run: bool = True, hours: int = 168):
    """One-off cleanup: for each post this user commented on MORE THAN ONCE in the last `hours`,
    delete the extra comments so exactly ONE remains. dry_run=True (default) only REPORTS what it
    would delete — pass dry_run=False to actually delete. Only real post URLs are actionable; feed
    comments logged under a synthetic key (no navigable URL) are reported as skipped."""
    dupes = get_duplicate_comment_posts(user_id, hours)
    if not dupes:
        return "No duplicate-commented posts found"

    actionable = [row for row in dupes if str(row[0]).startswith("http")]
    skipped = len(dupes) - len(actionable)
    if not actionable:
        return (f"Found {len(dupes)} duplicate-commented post(s) but none have a navigable URL "
                f"(synthetic feed keys) — cannot auto-delete; inspect manually.")

    try:
        driver, wait, user_email, my_profile = get_current_profile(
            user_id=user_id, session_name="Consolidate Comments")
    except Exception as e:
        log_error("Login failed for comment consolidation", exc=e, user_id=user_id,
                  task_name="consolidate_duplicate_comments_for_user")
        return f"Failed to start: {e}"

    posts_processed, total_found, total_deleted = 0, 0, 0
    try:
        for post_url, logged_count, first_at, last_at in actionable:
            try:
                driver.get(post_url)
                time.sleep(random.uniform(4, 7))
                found, deleted = _delete_extra_own_comments_on_post(driver, my_profile.full_name, dry_run)
                posts_processed += 1
                total_found += found
                total_deleted += deleted
                log_info(f"Consolidated comments on post (found={found}, deleted={deleted}, "
                         f"dry_run={dry_run})", user_id=user_id, post_id=post_url,
                         action_type="comment", task_name="consolidate_duplicate_comments_for_user")
            except Exception as e:
                log_warning("Failed to consolidate a post's comments", exc=e, user_id=user_id,
                            post_id=post_url, action_type="comment")
    finally:
        quit_gracefully(driver)

    verb = "would delete" if dry_run else "deleted"
    return (f"Processed {posts_processed} post(s); {verb} {total_deleted} extra comment(s) "
            f"(own comments found across posts={total_found}; skipped {skipped} non-URL post(s); "
            f"dry_run={dry_run})")


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id', 'post_id']},
                  queue='se_content')
def auto_seed_comment_on_post(self, user_id: int, post_id: int):
    """After the user's post publishes, leave a value-adding FIRST comment on it (an open question
    or a behind-the-scenes insight — no links) to seed the comment thread that drives reach, and
    beat LinkedIn's suppression of link-in-first-comment by adding real value instead.

    Posts via LinkedIn's socialActions API (w_member_social — the same token that publishes posts),
    NOT Selenium: commenting on the user's OWN post needs no browser and no login, so it is immune
    to the feed-navigation 429 rate limit. Everything it needs (post body, voice synthesis, profile,
    prefs) is read from the DB. Pinning is skipped here — LinkedIn exposes no pin API and the seed
    comment's thread-starting value stands without it.

    When the publish step held an external link back (issue #392 — C3), that link is appended to the
    comment: this is the delivery half of the link-in-first-comment mechanic."""
    post_url = get_post_url_from_log_for_user(user_id, post_id)
    if not post_url:
        return "No post URL yet for seed comment"
    object_urn = object_urn_from_post_url(post_url)
    if not object_urn:
        return f"Could not derive object URN from {post_url}"
    # Idempotency: a retried/re-dispatched task must not leave a SECOND comment on the same post
    # (duplicate own-comments are what consolidate_duplicate_comments_for_user exists to clean up).
    if has_user_commented_on_post_url(user_id, post_url):
        return "Seed comment already exists for this post"
    # Ground the AI in the canonical post body (posts table). Fall back to the log message only if
    # the post row is gone. Historical POST logs stored a status string, so grounding on the log
    # made seed comments read like they were about the /posts API instead of the post's subject.
    post_message = get_post_content(post_id) or get_post_message_from_log_for_user(user_id, post_id)
    if not post_message:
        return "No post content to seed a comment from"
    try:
        my_profile = load_profile_for_user(user_id)  # cached DB read — no scrape/login
        with llm_attribution(user_id=user_id, feature=FEATURE_COMMENT):
            seed = generate_seed_comment(post_message, my_profile, get_engagement_preferences(user_id),
                                         profile_synthesis=get_or_create_profile_synthesis(user_id, my_profile))
        # The generated comment never contains links (the prompt forbids them); the link held back at
        # publish time is appended deterministically here. A link on its own still ships when the
        # generator came back empty — losing the link entirely would be the worse failure.
        held_links = [l for l in (get_post_first_comment_link(post_id) or "").split("\n") if l.strip()]
        seed = append_link_to_comment(seed, held_links, post_id=post_id)
        if not seed:
            return "No seed comment generated"
        comment_urn = comment_on_linkedin_post(user_id, object_urn, seed)
        if comment_urn:
            insert_new_log(user_id=user_id, post_id=post_id, action_type=LogActionType.COMMENT,
                           result=LogResultType.SUCCESS, post_url=post_url, message=seed)
            myprint(f"Seed comment posted on post {post_id} via API ({comment_urn})")
            return f"Seed comment posted via API ({comment_urn})"
        return "Seed comment failed to post"
    except Exception as e:
        log_error("Seed comment error", exc=e, user_id=user_id, post_id=post_id, task_name="auto_seed_comment_on_post")
        return f"Seed comment error: {e}"


def _second_wave_story_directive(user_id: int, post_message: str, prefs: dict) -> tuple:
    """The story-bank injection for the second wave and the entry it came from (issue #620): the
    added insight must be the user's OWN material, so the writer gets one relevant entry and the
    hard rule that its facts are the only personal specifics it may state. An empty or irrelevant
    bank yields the explicit no-fabrication fallback rather than an invented anecdote."""
    try:
        entries = get_story_bank_entries(user_id, active_only=True)
    except Exception as e:
        log_warning("Story bank unavailable for the second-wave comment", exc=e, user_id=user_id)
        entries = []
    focus = (prefs or {}).get("focus_topics")
    story = _story_bank.select_story(entries, subject=str(post_message or "")[:300],
                                     focus_topics=focus if isinstance(focus, list) else None)
    return _story_bank.story_directive(story), story


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True,
                                                   'keys': ['user_id', 'post_id']})
def auto_second_wave_comment(self, user_id: int, post_id: int):
    """The SECOND WAVE (issue #622 / G7): 6–8 hours after publishing, add ONE self-comment that
    brings a substantive insight the post itself didn't carry — the evening re-surface of a post
    that is still earning engagement is a second distribution window, and a comment with real value
    in it is what re-opens the thread.

    Like the #344 seed comment it publishes through the socialActions API (no Selenium, no login),
    so it is immune to the feed-navigation 429 — but unlike the seed it IS discretionary
    amplification, so it stands down while automation is paused (the #629 suppression tripwire and
    any manual pause). The self-comment cap is enforced on the COUNT of our own comments on this
    post, so the seed and the second wave can never stack into thread-stuffing however they are
    re-dispatched. A draft that can't clear the comment quality contract ships NOTHING.

    The 6–8h wait is served in HOPS, not one long countdown: with `task_acks_late` the broker
    redelivers any message left unacked past `visibility_timeout` (~75 min), so a single 8-hour
    countdown would be handed to another worker every 75 minutes and post the comment several times
    over. Each run re-checks the post's real age and re-arms itself until the post is due."""
    if not _golden.second_wave_enabled():
        return "Second-wave comment disabled"
    due_minutes = _golden.second_wave_due_minutes(user_id, post_id)
    hop = _golden.second_wave_hop_seconds(
        due_minutes, _golden.latency_minutes(get_post_age_minutes(user_id, post_id)))
    if hop is not None:
        # Not due yet — re-arm inside the broker's visibility timeout. Checked BEFORE the pause so a
        # pause that lifts before the post is due doesn't cost it its second wave.
        auto_second_wave_comment.apply_async(kwargs={'user_id': user_id, 'post_id': post_id},
                                             countdown=hop)
        return f"Second wave not due yet ({due_minutes:.0f} min after publish) — re-armed in {hop}s"
    if is_automation_paused():
        reason = automation_pause_reason() or "automation paused"
        log_info(f"Second-wave comment skipped — {reason}", user_id=user_id, post_id=post_id,
                 action_type="comment", task_name="auto_second_wave_comment")
        return f"Skipped — {reason}"
    post_url = get_post_url_from_log_for_user(user_id, post_id)
    if not post_url:
        return "No post URL yet for the second-wave comment"
    object_urn = object_urn_from_post_url(post_url)
    if not object_urn:
        return f"Could not derive object URN from {post_url}"
    cap = _golden.self_comment_cap()
    already = count_user_comments_on_post_url(user_id, post_url)
    if already >= cap:
        return f"Self-comment cap reached ({already}/{cap}) for this post"
    post_message = get_post_content(post_id) or get_post_message_from_log_for_user(user_id, post_id)
    if not post_message:
        return "No post content to build a second-wave comment from"
    try:
        my_profile = load_profile_for_user(user_id)  # cached DB read — no scrape/login
        prefs = get_engagement_preferences(user_id)
        story_directive, story = _second_wave_story_directive(user_id, post_message, prefs)
        with llm_attribution(user_id=user_id, feature=FEATURE_COMMENT):
            comment = generate_second_wave_comment(
                post_message, my_profile, prefs=prefs,
                profile_synthesis=get_or_create_profile_synthesis(user_id, my_profile),
                story_directive=story_directive,
                recent_comments=list(get_recent_comment_texts(user_id)), user_id=user_id)
        if not comment:
            # The gate rejected every attempt. Nothing ships — a filler self-comment on our own post
            # costs more reach than the silence does.
            log_warning("Second-wave comment skipped — no draft cleared the quality contract",
                        user_id=user_id, post_id=post_id, action_type="comment",
                        task_name="auto_second_wave_comment")
            _record_golden_hour_report(user_id, post_id, 0,
                                       _reply_outcome("gate_failed", "no draft passed the gate"),
                                       phase=_golden.PHASE_SECOND_WAVE)
            return "No second-wave comment passed the quality gate"
        comment_urn = comment_on_linkedin_post(user_id, object_urn, comment)
        if not comment_urn:
            _record_golden_hour_report(user_id, post_id, 0,
                                       _reply_outcome("post_failed", "API rejected the comment"),
                                       phase=_golden.PHASE_SECOND_WAVE)
            return "Second-wave comment failed to post"
        insert_new_log(user_id=user_id, post_id=post_id, action_type=LogActionType.COMMENT,
                       result=LogResultType.SUCCESS, post_url=post_url, message=comment)
        # Recorded for visibility, NOT charged to the outbound envelope: like a reply on our own
        # post, this is presence on our own thread rather than discretionary outreach (#626).
        record_action(user_id, ACTION_REPLY)
        if story and story.get("id"):
            record_story_bank_use(user_id, story["id"])
        _record_golden_hour_report(user_id, post_id, 0,
                                   _reply_outcome("ok", "second wave posted", replies_sent=1),
                                   phase=_golden.PHASE_SECOND_WAVE)
        log_info(f"Second-wave comment posted on post {post_id} via API ({comment_urn})",
                 user_id=user_id, post_id=post_id, action_type="comment",
                 task_name="auto_second_wave_comment")
        return f"Second-wave comment posted via API ({comment_urn})"
    except Exception as e:
        log_error("Second-wave comment error", exc=e, user_id=user_id, post_id=post_id,
                  task_name="auto_second_wave_comment")
        return f"Second-wave comment error: {e}"


_ANALYTICS_URL = "https://www.linkedin.com/analytics/post-summary/{urn}/"


def _post_analytics_counts(driver, post_url: str) -> dict:
    """Counts from the author's own post-analytics page. Prefers the URN the detail page redirected
    to (the logged permalink holds a share/ugcPost URN; analytics keys off the activity URN LinkedIn
    resolves it to). Best-effort — {} when no URN, no page, or nothing parseable."""
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
    falls back to the defaults rather than taking the whole sweep down."""
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
    signals the detail page never renders (saves, impressions)."""
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
    string when the page can't be read — never raises."""
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
    daily capture, or anything downstream of it, down with it."""
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
    feeds the growth panel's 7/30-day deltas; unreadable signals are stored as NULL."""
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


def _enumerate_joined_groups(driver) -> list:
    """Scrape the user's joined groups from /groups/ → list of (group_id, name). Best-effort."""
    driver.get("https://www.linkedin.com/groups/")
    time.sleep(random.uniform(5, 8))
    for y in (600, 1200, 1800):
        driver.execute_script(f"window.scrollTo(0,{y});")
        time.sleep(1.5)
    items = driver.execute_script(
        "const out=[]; const seen=new Set();"
        "for(const a of document.querySelectorAll(\"a[href*='/groups/']\")){"
        "  const m=(a.getAttribute('href')||'').match(/\\/groups\\/(\\d+)/); if(!m) continue;"
        "  const id=m[1]; if(seen.has(id)) continue;"
        "  const name=(a.innerText||'').trim().split('\\n')[0];"
        "  if(name && name.length>1){ seen.add(id); out.push([id, name.slice(0,255)]); }"
        "} return out.slice(0,60);")
    return [(str(i[0]), i[1]) for i in (items or []) if i and i[0]]


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_content')
def auto_sync_user_groups(self, user_id: int):
    """Refresh the user's joined-groups list (new groups default to enabled)."""
    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Sync Groups")
    except Exception as e:
        log_error("Error getting profile for group sync", exc=e, user_id=user_id, task_name="auto_sync_user_groups")
        return f"Failed: {e}"
    try:
        groups = _enumerate_joined_groups(driver)
        for gid, name in groups:
            upsert_user_group(user_id, gid, name)
        return f"Synced {len(groups)} group(s)"
    finally:
        quit_gracefully(driver)


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_engage')
def auto_comment_in_groups(self, user_id: int, max_per_group: int = 2):
    """Comment (value-add, scored) on posts in each of the user's ENABLED groups. Reuses the feed
    commenting engine pointed at each group's feed. Shares the per-day comment cap."""
    enabled = get_enabled_group_ids(user_id)
    if not enabled:
        return "No enabled groups"
    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Group Commenting")
    except Exception as e:
        log_error("Error getting profile for group commenting", exc=e, user_id=user_id, task_name="auto_comment_in_groups")
        return f"Failed: {e}"
    prefs = get_engagement_preferences(user_id)
    engagers = get_recent_engagers(user_id)
    total = 0
    try:
        for gid in enabled:
            driver.get(f"https://www.linkedin.com/groups/{gid}/")
            time.sleep(random.uniform(4, 7))
            total += comment_on_feed_inline(driver, wait, my_profile, user_id,
                                            max_posts=max_per_group, prefs=prefs, engagers=engagers)
        return f"Commented {total} time(s) across {len(enabled)} group(s)"
    finally:
        quit_gracefully(driver)


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id', 'group_id']},
                  queue='se_content')
def auto_post_to_group(self, user_id: int, group_id: str, group_name: str = None):
    """Publish one short, value-add (non-promotional) post into a group via its share box. The text
    is written FRESH for that group — a group post is never a copy or reshare of a scheduled feed
    post. Best-effort — the group composer selectors are validated in the live pass."""
    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Group Post")
    except Exception as e:
        log_error("Error getting profile for group post", exc=e, user_id=user_id, task_name="auto_post_to_group")
        return f"Failed: {e}"
    try:
        driver.get(f"https://www.linkedin.com/groups/{group_id}/")
        time.sleep(random.uniform(4, 7))
        with llm_attribution(user_id=user_id, feature=FEATURE_CONTENT):
            text = _strip_non_bmp(generate_group_post(
                my_profile, group_name=group_name, prefs=get_engagement_preferences(user_id),
                profile_synthesis=get_or_create_profile_synthesis(user_id, my_profile)) or "")
        if not text.strip():
            return "No group post generated"

        def _unpostable(reason: str) -> str:
            # The group loaded but its composer did not: members cannot post here (admin-only /
            # announcement group). Stamp the RUN so the rotation moves past it — ordering on
            # successful posts alone left such a group "least recently posted" forever, starving
            # every other post-enabled group (issue #858). `last_posted_at` is untouched, so it
            # stays the truthful record of what actually shipped.
            record_group_post_run(user_id, group_id)
            log_info(f"Group is not postable, rotating past it: {reason}", user_id=user_id,
                     task_name="auto_post_to_group")
            return reason

        # Open the group share box, type, and post (best-effort SDUI selectors).
        if click_first(driver, wait, [(By.XPATH,
                "//button[contains(normalize-space(),'Start a post') or contains(normalize-space(),'Start a public post') "
                "or contains(@aria-label,'Start a post') or contains(@aria-label,'Create a post') "
                "or (contains(normalize-space(),'Start a') and contains(normalize-space(),'post'))]")],
                       "Group share box", required=False) is None:
            return _unpostable("Group share box not found")
        time.sleep(random.uniform(2, 3))
        box = find_first(driver, wait, [(By.CSS_SELECTOR, "div[role='textbox']")], "Group post editor",
                         visible_only=True, required=False)
        if box is None:
            return _unpostable("Group post editor not found")
        box.click()
        box.send_keys(text)
        time.sleep(random.uniform(1, 2))
        if click_first(driver, wait, [(By.XPATH, "//button[normalize-space()='Post']")], "Group Post button",
                       required=False) is None:
            return _unpostable("Group Post button not found")
        time.sleep(random.uniform(3, 5))
        # Only a post that actually shipped advances the rotation — a failed run leaves this group
        # next in line rather than skipping its turn.
        record_group_post(user_id, group_id)
        return "Posted to group"
    except Exception as e:
        log_error("Group post error", exc=e, user_id=user_id, task_name="auto_post_to_group")
        return f"Error: {e}"
    finally:
        quit_gracefully(driver)


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_engage')
def automate_commenting(self, user_id: int, loop_for_duration: int = None, future_forward: int = 60,
                        post_id: int = None):
    """Walk the feed and comment. `post_id` is set only by the pre-post warm-up dispatch
    (auto_check_scheduled_posts) — it makes each pass record a per-post engagement-window marker so
    a report can confirm the warm-up before that post actually happened (issue #547). It rides the
    self-requeue kwargs, so every pass in the window accumulates onto the same marker."""
    global stop_all_thread

    myprint("Starting Automate Commenting Thread...")

    # Comment-quality hold (issue #628): when the weekly outcome report finds our comments are being
    # demoted out of 'Most relevant', commenting stops for this user until a human clears it.
    # Checked here (not in the dispatcher) so EVERY caller — golden hour, pre-post warm-up, the
    # self-requeue — is covered by one gate. Fails open when Redis is unavailable.
    if is_commenting_held(user_id):
        reason = commenting_hold_reason(user_id) or "comment quality"
        log_warning(f"Feed commenting held for user {user_id}: {reason}", user_id=user_id,
                    action_type="comment", task_name="automate_commenting")
        return f"Skipped: feed commenting is held ({reason})"

    # Single-flight per user: the pre-post trigger, the golden-hour beat, and this task's own
    # self-requeue can otherwise run concurrently and double-walk the feed. Only one commenting
    # run per user at a time; a loser skips this cycle (its own re-schedule will pick it up).
    lock_name = f"automate_commenting:{user_id}"
    lock_token = acquire_run_lock(lock_name, ttl_seconds=1800)
    if lock_token is None:
        myprint(f"Another commenting run is in progress for user {user_id} — skipping this cycle.")
        return "Skipped: another commenting run already in progress for this user."

    if post_id:
        log_info("Pre-post engagement window opened", post_id=post_id, user_id=user_id,
                 task_name="automate_commenting")

    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Auto Commenting")
    except Exception as e:
        log_error("Error while getting profile for auto commenting", exc=e, user_id=user_id, task_name="automate_commenting")
        release_run_lock(lock_name, lock_token)
        return f"Failed to start auto commenting: {e}"

    result = "Automate Commenting Task Started"

    try:

        navigate_to_feed(driver, wait)

        start_time = datetime.now()
        deadline_ts = (start_time.timestamp() + loop_for_duration) if loop_for_duration else None

        # Comment inline on the SDUI feed (no per-post permalink navigation anymore).
        post_commented_count = comment_on_feed_inline(driver, wait, my_profile, user_id,
                                                      max_posts=10, deadline_ts=deadline_ts)

        result = f"Automate Commenting Task Completed. Commented on {post_commented_count} posts."

        if post_id:
            record_pre_post_run(post_id, user_id, post_commented_count)

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
                myprint(f"Loop duration reached. Stopping {current_function_name} task...")
            else:
                # Change the value of the loop_for_duration parameter
                kwargs['loop_for_duration'] = new_loop_for_duration
                # Add our function call back to the task queue
                myprint(f"Adding {current_function_name} back to queue for {future_forward} seconds in the future...")
                # Remove 'self' from kwargs if it exists
                if 'self' in kwargs:
                    del kwargs['self']
                # Call self again in the future
                globals()[current_function_name].apply_async(kwargs=kwargs, countdown=future_forward)

    except Exception as e:
        log_error("Error while automating commenting", exc=e, user_id=user_id, task_name="automate_commenting")
        result = f"Error while automating commenting: {e}"
    finally:
        quit_gracefully(driver)  # Close the driver
        # Released here (after this run's feed walk); the self-requeue fires 60s later as a fresh
        # task and re-acquires. A crash still frees the lock via its TTL.
        release_run_lock(lock_name, lock_token)

    return result


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


def _profile_slug(profile_url: str) -> str:
    """The /in/<slug> identity from a profile URL — the stable half of a lead's dedup key."""
    m = re.search(r"/in/([^/?#]+)", profile_url or "")
    return (m.group(1).lower() if m else "")


def _reply_target_key(user_id: int, post_id: int, commenter_slug: str, comment_text: str) -> str:
    """Stable Redis key for a specific comment on a specific post so the reply sweep never replies
    to the SAME comment twice across golden-hour sweeps. Keyed on identity (commenter slug) + a
    normalized prefix of the comment text — the same inputs the follow-up path uses for its dedup.
    If we cannot resolve the commenter we fall back to a text-only hash so the dedup still has a
    chance to prevent duplicates."""
    text_norm = _normalize_post_text(comment_text)[:200]
    who = (commenter_slug or "").strip().lower()
    if not who:
        who = hashlib.sha1(text_norm.encode("utf-8", "ignore")).hexdigest()[:16]
    digest = hashlib.sha1(f"{who}|{text_norm}".encode("utf-8", "ignore")).hexdigest()[:20]
    return f"linkedin:replied_to_own_comment:{user_id}:{post_id}:{digest}"


def _has_replied_to_comment(user_id: int, post_id: int, commenter_slug: str, comment_text: str) -> bool:
    """True if we have already recorded a reply to this comment (best-effort Redis check; fails
    open when Redis is unavailable)."""
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
    window plus a small buffer; clamped so a misconfigured value can't pin keys forever."""
    client = _redis_client()
    if client is None:
        return
    ttl_seconds = max(1, min(15 * 24 * 60 * 60, int(ttl_days) * 24 * 60 * 60))
    try:
        client.set(_reply_target_key(user_id, post_id, commenter_slug, comment_text), "1", ex=ttl_seconds)
    except Exception as e:
        log_warning("Could not record replied-to-comment marker", exc=e, user_id=user_id,
                    post_id=post_id, action_type="reply")


def _lead_thread_key(source: str, thread_ref: str, person_profile_url: str, person_name: str = "") -> str:
    """Stable dedup id for ONE conversation with ONE person, so a re-scan (or a second buying-intent
    line in the same thread) never re-flags a lead the operator has already seen. Keyed on identity
    + thread — never on the message text (the #474 lesson)."""
    who = _profile_slug(person_profile_url)
    if not who:
        who = hashlib.sha1((person_name or "").strip().lower().encode("utf-8", "ignore")).hexdigest()[:12]
    return f"lead:{source}:{thread_ref}:{who}"


def _flag_lead_signal(user_id: int, text: str, source: "LeadSignalSource", thread_ref: str,
                      person_name: str = None, person_profile_url: str = None,
                      channel: "LeadSignalChannel" = LeadSignalChannel.REPLY,
                      post_id: int = None, context_url: str = None, context_text: str = None,
                      my_profile=None, prefs: dict = None, profile_synthesis: str = None) -> "int | None":
    """Classify one piece of inbound text and, on a buying-intent hit, queue it as a hot lead with a
    drafted response awaiting approval. Returns the new signal id, or None (no intent, already
    flagged, or an error). Best-effort and NON-FATAL — lead detection must never break a reply sweep."""
    try:
        if not text or not str(text).strip():
            return None
        key = _lead_thread_key(str(source), thread_ref, person_profile_url or "", person_name or "")
        if has_lead_signal(user_id, key):
            return None
        verdict = detect_lead_signals(text)
        if not verdict.get("is_lead"):
            return None
        draft = None
        try:
            with llm_attribution(user_id=user_id, feature=FEATURE_DM):
                draft = generate_lead_response(text, my_profile, channel=str(channel), context=context_text,
                                               prefs=prefs, profile_synthesis=profile_synthesis)
        except Exception as e:
            log_warning("Lead response draft failed; queueing the signal without one", exc=e,
                        user_id=user_id, action_type="engaged")
        signal_id = insert_lead_signal(
            user_id, source, key, person_name=person_name, person_profile_url=person_profile_url,
            snippet=str(text)[:2000], score=int(verdict.get("score") or 0),
            matched_signals=",".join(verdict.get("matched") or [])[:512], post_id=post_id,
            context_url=context_url, draft_response=draft, channel=channel)
        if signal_id:
            log_info(f"Hot lead detected ({verdict.get('method')}, score {verdict.get('score')}) "
                     f"from {person_name or person_profile_url or 'unknown'}",
                     user_id=user_id, post_id=post_id, action_type="engaged")
        return signal_id
    except Exception as e:
        log_warning("Lead-signal detection failed", exc=e, user_id=user_id, action_type="engaged")
        return None


def _reply_outcome(status: str, summary: str, comments_found: int = 0,
                   replies_sent: int = 0) -> dict:
    """One post's reply-sweep outcome. A dict, not a string, because the golden-hour report
    (issue #622) needs the counts the old summary line only ever rendered."""
    return {"status": status, "summary": summary, "comments_found": int(comments_found),
            "replies_sent": int(replies_sent)}


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
    delivery that can't be drafted must never break the reply sweep it rides on."""
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
    has a live driver/profile."""
    post_url = get_post_url_from_log_for_user(user_id, post_id)
    if not post_url:
        myprint(f"No successful post URL for post {post_id}; skipping replies.")
        return _reply_outcome("no_post_url", "No post URL")
    # Ground replies in the canonical post body (posts table); fall back to the log message.
    post_message = get_post_content(post_id) or get_post_message_from_log_for_user(user_id, post_id)

    log_info("Replying to Comments of Post ID", post_id=post_id)
    if driver.current_url != post_url:
        driver.get(post_url)

    # SDUI: expand more replies where available, then collect comment items from the
    # new data-testid comment list (comments are no longer article.comments-comment-entity).
    for _ in range(5):
        more = click_first(driver, wait,
                           [(By.XPATH, "//button[contains(@aria-label,'more comment') or "
                                       "contains(normalize-space(),'Load more') or "
                                       "contains(normalize-space(),'more repl')]")],
                           "Load more comments", required=False)
        if not more:
            break
        time.sleep(2)

    comments = _comment_items_from_thread(driver)
    myprint(f"Comments Found: {len(comments)}")

    # our profile slug — used to detect comments we AUTHORED or already replied to (the loop-breaker).
    our_slug = _profile_slug(str(my_profile.profile_url))
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
            myprint(f"Reply cap reached ({_MAX_REPLIES_PER_SWEEP}) for post {post_id}")
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
            commenter_slug = _profile_slug(_eprofile)
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
            pass
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


def _record_golden_hour_report(user_id: int, post_id: int, sweep_slot: int, outcome: dict,
                               phase: str = _golden.PHASE_REPLY_SWEEP) -> "dict | None":
    """Build, log and ship ONE post's golden-hour report (issue #622), or None for a post too old to
    be the amplifier's business. Latency is measured from the post's real publish time, so the
    report answers the audit's question — did this sweep land inside the window, and did it find
    anything? A sweep that arrived late is logged as a WARNING: that is the queue-backlog signal the
    #401 audit had no way to see."""
    outcome = dict(outcome or {})
    try:
        minutes = _golden.latency_minutes(get_post_age_minutes(user_id, post_id))
    except Exception as e:
        # Measurement must never break the thing it measures — an unreadable age reports as unknown
        # (and therefore out-of-window), it does not abort the sweep mid-post.
        log_warning("Golden-hour latency unreadable", exc=e, user_id=user_id, post_id=post_id)
        minutes = None
    # Scoped to the phase's own horizon: the sweep also walks older posts by design, and grading
    # those revisits would bury the real reading under permanent out-of-window noise.
    if not _golden.should_report(minutes, phase):
        return None
    report = _golden.golden_hour_report(
        post_id, minutes, comments_found=outcome.get("comments_found"),
        replies_sent=outcome.get("replies_sent"), status=outcome.get("status") or "ok",
        sweep_slot=sweep_slot, phase=phase)
    summary = _golden.report_summary(report)
    second_wave = phase == _golden.PHASE_SECOND_WAVE
    task_name = "auto_second_wave_comment" if second_wave else "sweep_reply_comments"
    action_type = "comment" if second_wave else "reply"
    if report["within_window"]:
        log_info(summary, user_id=user_id, post_id=post_id, action_type=action_type,
                 task_name=task_name)
    else:
        # Out of window on a post young enough to still matter (should_report already dropped the
        # older ones) — that is the queue-backlog / rate-limit signal, so it goes out as a WARNING.
        log_warning(summary, user_id=user_id, post_id=post_id, action_type=action_type,
                    task_name=task_name)
    try:
        track_golden_hour_report(user_id, report)
    except Exception as e:
        log_warning("Golden-hour report not tracked", exc=e, user_id=user_id, post_id=post_id)
    return report


def _retry_golden_hour_sweep(user_id: int, sweep_slot: int, attempt: int, status: str) -> bool:
    """A golden-hour sweep that could not run at all REPORTS itself and then gets one more chance
    while the window is still open — the #401 amplifier used to lose the whole hour to a single
    transient 429, and the audit could never tell that apart from a post nobody commented on.

    The report is what makes the difference visible: it carries `status` ("rate_limited",
    "session_failed") against the freshest post's latency, so a silent hour has a cause in PostHog
    instead of an absence. Returns True when a retry was scheduled — bounded by
    `sweep_retry_countdown` (attempts AND window), so a sustained rate-limit decays to nothing
    instead of hammering LinkedIn."""
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
    still dedups against a concurrently-queued one."""
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
        myprint(f"Another reply sweep is already running for user {user_id} — skipping.")
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
    feedpost:// hash keys (not navigable) or anything unrecognized."""
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
    separately — this only gates whether we also post a reply."""
    if not text:
        return False
    stripped = re.sub(r"https?://\S+", " ", text)
    return "?" in stripped


def _followup_reply_key(post_key: str, replier_href: str, reply_text: str) -> str:
    """Stable dedup id for a specific reply so we react/reply to it AT MOST ONCE (the #474 lesson —
    key on identity, not raw text). Anchored to the post, the replier's profile slug, and a
    NORMALIZED hash of the reply text."""
    slug = ""
    if replier_href:
        m = re.search(r"/in/([^/?#]+)", replier_href)
        slug = (m.group(1).lower() if m else "")
    norm = _normalize_post_text(reply_text)[:200]
    digest = hashlib.sha1(f"{slug}|{norm}".encode("utf-8", "ignore")).hexdigest()[:16]
    return f"{post_key}#reply:{slug}:{digest}"


# SDUI comment thread (validated live 2026-07-24 on a moderated group post, issue #478):
#   * comments render as [data-testid='expandable-text-box'] INSIDE [data-testid*='commentList']
#     — but ONLY once scrolled into view (a long post pushes them far below the fold);
#   * a comment's author is the header /in/ link that is NOT inside the text box (an @mention in a
#     reply body is also an /in/ link — that was the false "mine" match);
#   * replies are nested inside their parent comment's container (DOM containment);
#   * the like control is a button whose aria-label starts "React " (e.g. "React Like"); the reply
#     control is aria-label="Reply". "…more" truncates long replies until expanded.
_COMMENTLIST_TEXTBOX = "[data-testid*='commentList'] [data-testid='expandable-text-box']"


def _comment_header_author(driver, container) -> str:
    """A comment's author profile href from its HEADER link — never an @mention inside the body
    text box (that false match flagged a reply that mentioned us as 'ours')."""
    try:
        return driver.execute_script(
            "const c=arguments[0];"
            "for(const a of c.querySelectorAll(\"a[href*='/in/']\")){"
            "  if(!a.closest(\"[data-testid='expandable-text-box']\")) return (a.href||'').split('?')[0];"
            "}return '';", container) or ""
    except Exception:
        return ""


def _comment_container(driver, textbox):
    """Smallest ancestor of a comment text box that carries a HEADER author link and is not the
    post wrapper (which uniquely has the GIF/Repost/Emoji composer buttons)."""
    try:
        return driver.execute_script(
            "let el=arguments[0],d=0;while(el&&d<10){"
            " const hdr=[...el.querySelectorAll(\"a[href*='/in/']\")].some(a=>!a.closest(\"[data-testid='expandable-text-box']\"));"
            " const post=[...el.querySelectorAll('button')].some(b=>/GIF|Repost|Emoji Picker/.test(b.getAttribute('aria-label')||''));"
            " if(hdr&&!post) return el; el=el.parentElement;d++;}return null;", textbox)
    except Exception:
        return None


def _react_to_comment_inline(driver, wait, comment_el, user_id: int = None) -> bool:
    """Like a comment/reply (best-effort, non-fatal). The action bar is HOVER-HIDDEN (the react
    button is zero-size until the comment is hovered), so hover first, then click the react control
    ('Open reactions menu' on this SDUI; a single click applies the default Like). If the click
    opens the reaction flyout instead, pick the visible 'Like'. Skips if already reacted."""
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


def _reply_under_comment_inline(driver, wait, comment_el, reply_text: str, user_id: int = None) -> bool:
    """Reply UNDER a specific comment — NOT as a new top-level comment. The bug: clicking a comment's
    Reply then taking the first page-wide role=textbox grabbed the post's main 'Add a comment' box, so
    the reply posted as a standalone comment (#478).

    #478's own fix only PENALISED a composer above the comment, so the main box still won when it was
    the only visible one — the exact failure this function exists to prevent (#886). Composer
    resolution is now `_reply_composer_for_comment`, shared with `_reply_to_comment_inline` (#883):
    a box inside this comment wins, a box above it is rejected outright, a box owned by a DIFFERENT
    comment is rejected, and no box of ours means skip. This function keeps only its own way of
    OPENING the box — the #478 thread path needs the scroll + hover that renders a hover-hidden Reply
    button before it can be clicked."""
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", comment_el)
        try:
            ActionChains(driver).move_to_element(comment_el).pause(0.5).perform()  # reveal action bar
        except Exception:
            pass  # hover is best-effort; the Reply button lookup below still runs
        rbtns = comment_el.find_elements(By.CSS_SELECTOR, "button[aria-label='Reply']")
        if not rbtns:
            log_warning("Reply-under-comment: no Reply button found", action_type="reply", user_id=user_id)
            return False
        try:
            ActionChains(driver).move_to_element(rbtns[0]).pause(0.2).click(rbtns[0]).perform()
        except Exception:
            driver.execute_script("arguments[0].click();", rbtns[0])
        time.sleep(random.uniform(1.5, 2.8))
        composer = _reply_composer_for_comment(driver, comment_el, user_id=user_id)
        if composer is None:
            return False  # expected no-op (the box never opened) — `_reply_composer_for_comment` logs it DEBUG
        return _type_and_submit_reply(driver, composer, reply_text, user_id=user_id)
    except Exception as e:
        log_warning("Reply-under-comment failed", exc=e, action_type="reply", user_id=user_id)
        return False


def _load_comment_thread(driver) -> None:
    """Make a post's comment thread actually render: a TALL viewport is what lazy-renders comments a
    long post pushes far below the fold (scrolling alone on the default 1080-tall window did not —
    validated live, issue #478), then scroll down and expand the '…more' / 'previous replies'
    controls. Best-effort throughout; every step is optional."""
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


def _comment_items(driver) -> list:
    """[(text_box, container, author_href)] for every comment/reply currently rendered in the
    thread. Text boxes with no resolvable container are dropped — a comment we can't scope to a
    container has no author and no action bar, so it is not addressable."""
    items = []
    for tb in driver.find_elements(By.CSS_SELECTOR, _COMMENTLIST_TEXTBOX):
        cont = _comment_container(driver, tb)
        if cont is None:
            continue
        items.append((tb, cont, _comment_header_author(driver, cont)))
    return items


def _followup_on_post_comment_replies(driver, wait, user_id: int, post_url: str, post_key: str,
                                      my_profile, profile_synthesis: str, prefs: dict,
                                      replies_remaining: int) -> dict:
    """Revisit ONE post we commented on: react to replies to our comment, answer question-replies,
    and flag buying intent. Returns {'reacted': n, 'replied': n, 'leads': n}. Best-effort/non-fatal
    (issues #478, #483)."""
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
    automated comments (the commented_posts ledger). QueueOnce + single-flight lock + 429-safe."""
    return _run_comment_followups_sweep(user_id)


def _run_comment_followups_sweep(user_id: int) -> str:
    """Body of sweep_comment_followups, extracted so it is unit-testable without the QueueOnce/Redis
    task wrapper."""
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
    questions, same as the sweep (issue #478)."""
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
    a supervised run (issue #478)."""
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
    comment to a ledger row by our comment text, then upgrades the key to feedurn:// (issue #478)."""
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
    demoted — the one reading this feature exists to produce."""
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


def _sort_from_element(el) -> str:
    """The sort an element names ('most relevant' / 'most recent'), or '' when it names neither —
    which is how a wrong-but-matching node is told apart from the real control."""
    try:
        text = f"{el.get_attribute('aria-label') or ''} {el.text or ''}".lower()
    except Exception:
        return ""
    if _SORT_MOST_RECENT in text:
        return _SORT_MOST_RECENT
    if _SORT_MOST_RELEVANT in text:
        return _SORT_MOST_RELEVANT
    return ""


def _find_comment_sort_control(driver, wait):
    """The comment sort control, preferring a candidate whose own label actually reads as a sort.

    find_first hands back the first match of the first locator that yields ANY element — it never
    looks at what it found. One unrelated 'sort' button matched by a broad fallback would therefore
    be returned forever, read as unreadable, and never warn ('Selector miss' only fires on a TOTAL
    miss), which is exactly the starved denominator #818 is about. Walking the chain here lets such
    a node fall through to a locator that names the real sort. Some renders label the control only
    inside its popup, so an unvalidated candidate is still returned when nothing in the chain parses
    — that is the pre-existing behaviour, and the click path in `_switch_comment_sort` needs it."""
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
    return find_first(driver, wait, _COMMENT_SORT_LOCATORS, "Comment sort control", required=False)


def _comment_sort_label(driver, wait) -> str:
    """The comment sort currently applied, lowercased ('most relevant' / 'most recent'), or '' when
    the control isn't present. '' is load-bearing: without knowing the sort we cannot say whether an
    absent comment was demoted, so the visibility reading stays NULL rather than guessing."""
    try:
        btn = _find_comment_sort_control(driver, wait)
    except Exception:
        return ""
    return _sort_from_element(btn) if btn is not None else ""


def _switch_comment_sort(driver, wait, target: str = _SORT_MOST_RECENT) -> bool:
    """Best-effort flip of the comment sort. True only when the control afterwards reports `target`
    — an unverified flip would let 'not found here' be read as a demotion when the sort never
    actually changed."""
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
    text we stored. Empty on either side never matches — two blank reads must not collide."""
    a = _norm_prefix(rendered, _COMMENT_MATCH_PREFIX_CHARS)
    b = _norm_prefix(logged, _COMMENT_MATCH_PREFIX_CHARS)
    if not a or not b:
        return False
    return a.startswith(b) or b.startswith(a)


def _href_is_profile(href: str, slug: str) -> bool:
    """True when a profile href belongs to EXACTLY `slug`. A substring test — `f"/in/{slug}" in
    href` — also matches every slug ours is a PREFIX of ('/in/chris' inside '/in/chris-queen-9b1'),
    which would let a stranger's comment be read as ours and their reply be discounted as our own."""
    if not slug:
        return False
    return _profile_slug(href or "") == str(slug).strip().lower()


def _find_our_comment(items: list, our_slug: str, comment_text: str):
    """Our comment's container within a rendered thread, or None.

    Falls back to the single container authored by us when the text doesn't match: the
    commented_posts ledger makes our automated commenting at-most-once per post, so one comment of
    ours on the post IS this comment even when an @mention or emoji re-render broke the prefix
    match. With several of ours present (a manual comment, a reply) the text has to decide."""
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
    itself, so our own container is excluded explicitly."""
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
    that is NOT inside the comment list (a commenter's link would otherwise win)."""
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
    sort_label = _comment_sort_label(driver, wait)
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

    replies = _thread_replies(driver, ours, items)
    author_href = _post_author_href(driver)
    author_slug = _profile_slug(author_href)
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
    (issue #628). Read-only on LinkedIn: it navigates and reads, it never comments or reacts."""
    return _run_comment_outcomes_sweep(user_id)


def _run_comment_outcomes_sweep(user_id: int) -> str:
    """Body of sweep_comment_outcomes, extracted so it is unit-testable without the QueueOnce/Redis
    task wrapper."""
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
        our_slug = _profile_slug(str(my_profile.profile_url))
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
    429-safe: a rate-limited session returns cleanly instead of dying before the re-queue."""

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
                myprint(f"Loop duration reached. Stopping {current_function_name} task...")
            else:
                # Change the value of the loop_for_duration and future_forward parameters
                kwargs['loop_for_duration'] = new_loop_for_duration
                kwargs['future_forward'] = future_forward
                # Add our function call back to the task queue
                myprint(
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
    card in place instead of removing it."""
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


def get_recent_recommendations(driver, wait) -> dict[str, str]:
    """Return {profile_url: name} for users who recently recommended the current user.

    Scrapin the LinkedIn Recommendations Received section is not yet implemented;
    returns an empty dict until a scraper is added here.
    """
    return {}


def get_recent_collaborators(driver, wait) -> dict[str, str]:
    """Return {profile_url: name} for recent collaborators to thank.

    No structured LinkedIn data source exists for this yet; returns an empty dict
    until a notification-scraper is added here.
    """
    return {}


class _SafePlaceholders(dict):
    """format_map backing dict that leaves unknown {tokens} literal instead of raising —
    so a user typo like {frst_name} never drops the whole message."""
    def __missing__(self, key):
        return "{" + key + "}"


def render_dm_placeholders(text: str, *, first_name: str = "", headline: str = "",
                           blog_url: str = "", event_detail: str = "") -> str:
    """Single source of truth for filling DM / lead-magnet {placeholders}: {first_name},
    {headline}, {blog_url}, {event_detail}. Used by BOTH the DM-template path and the Comment->DM
    lead magnet so their substitution can never drift. Tolerates unknown/malformed tokens gracefully."""
    if not text:
        return text or ""
    ctx = _SafePlaceholders(first_name=first_name or "there",
                            headline=headline or "my professional field",
                            blog_url=blog_url or "",
                            # Catch-up templates (issue #482) reference the specific milestone; the
                            # fallback keeps the sentence grammatical if the detail didn't scrape.
                            event_detail=event_detail or "the news")
    try:
        return text.format_map(ctx)
    except (IndexError, ValueError):
        # malformed/positional braces (e.g. a stray "{") — replace known tokens only
        out = text
        for k in ("first_name", "headline", "blog_url", "event_detail"):
            out = out.replace("{" + k + "}", str(ctx[k]))
        return out


@attribute_llm_cost(FEATURE_DM)
def build_dm_from_template(user_id: int, event_type: str, first_name: str,
                           my_profile: LinkedInProfile, step: int = 0, blog_url: str = "",
                           event_detail: str = "") -> "str | None":
    """Render the user's DM template for an event (filling {first_name}/{headline}/{blog_url}/
    {event_detail}) and LLM-refine it to their voice (<=300 chars). Falls back to the code-default
    template; returns None only when no template exists for that (event, step)."""
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
    every sequence."""
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
    doesn't match (detection simply finds nothing rather than breaking the follow-up run)."""
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
    all keep the human in the loop. This is the one flag where a typo must fail CLOSED."""
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

    Best-effort and NON-FATAL — nurturing a conversation must never break the follow-up run."""
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
    gets the next-step template rendered in the user's voice, sent, marked, and re-scheduled."""
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


def _dispatch_appreciation_dms(user_id: int, my_profile: LinkedInProfile, event_type: str,
                               recipients: dict) -> int:
    """Send the step-0 appreciation DM for one trigger event to everyone it fired for, and put each
    thread on the follow-up sequencer. Returns how many were dispatched. A missing template used to
    drop the recipient in silence — now it says so, because a template gap here is the difference
    between a nurture pipeline and an empty one (issue #623)."""
    sent = 0
    for profile_url, name in (recipients or {}).items():
        first_name = clean_person_name(name).split(" ")[0] or "there"
        message = build_dm_from_template(user_id, event_type, first_name, my_profile)
        if not message:
            log_warning(f"No '{event_type}' DM template for step 0 — skipping the appreciation DM "
                        f"to {first_name}", user_id=user_id, action_type="dm")
            continue
        send_private_dm.apply_async(kwargs={"user_id": user_id, "profile_url": profile_url,
                                            "message": message})
        enqueue_next_followup(user_id, profile_url, first_name, event_type, 0)
        sent += 1
    return sent


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  reject_on_worker_lost=True, rate_limit='2/m', queue='se_outreach')
def automate_appreciation_dms_for_user(self, user_id: int, loop_for_duration: int = None, future_forward: int = 60):
    user_email, user_password = get_user_password_pair_by_id(user_id)

    driver, wait = get_driver_wait_pair(session_name='Appreciation DMs', user_id=user_id)

    try:
        login_to_linkedin(driver, wait, user_email, user_password)

        start_time = datetime.now()

        myprint("Sending Appreciations here...")

        result = "Appreciation DMs Sent"

        my_profile = load_profile_for_user(user_id)  # cached DB read — only supplies {headline}

        # After Accepting a Connection Request:
        invitations_accepted = accept_connection_request(user_id)
        _dispatch_appreciation_dms(user_id, my_profile, "connection_accepted", invitations_accepted)

        # After Receiving a Recommendation — thank the recommender
        _dispatch_appreciation_dms(user_id, my_profile, "recommendation_received",
                                   get_recent_recommendations(driver, wait))

        # After a Successful Collaboration — express gratitude and offer to connect further
        _dispatch_appreciation_dms(user_id, my_profile, "collaboration",
                                   get_recent_collaborators(driver, wait))

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
                myprint(f"Loop duration reached. Stopping {current_function_name} task...")
            else:
                # Change the value of the loop_for_duration parameter
                kwargs['loop_for_duration'] = new_loop_for_duration
                # Add our function call back to the task queue
                myprint(f"Adding {current_function_name} back to queue for {future_forward} seconds in the future...")
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
    if post_link != driver.current_url:
        # Switch to post url
        driver.get(post_link)

    # Get my user_id
    user_id = get_user_id(my_profile.email)

    # Check to make sure user hasn't already commented on this post
    if check_commented(driver, wait, user_id, post_link):
        myprint("Already commented on this post. Skipping...")
        return False  # Skip posts we've already commented on
    else:
        myprint("Haven't commented on this post yet. Proceeding...")

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
    myprint(f"Simulated Reading... for {read_time:.0f} seconds")

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

    myprint(f"AI Generated Comment: {comment_text}")
    # Simulate typing the AI-generated comment
    # for char in comment_text:
    #    if char == '\n':
    #        myprint()
    #    else:
    #        myprint(char, end='')
    #    time.sleep(random.uniform(0.05, 0.15))  # Simulate human typing speed

    # Comment out the actual posting of the comment for now
    kwargs = {'user_id': get_user_id(my_profile.email),
              'post_link': post_link,
              'comment_text': comment_text}
    comment_on_post.apply_async(kwargs=kwargs)

    myprint(f"Comment Posted on: {post_link}")

    return True


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_outreach')
def automate_profile_viewer_engagement(self, user_id: int, loop_for_duration: int = None, future_forward: int = 60):
    global stop_all_thread

    myprint("Starting Profile Viewer DMs")

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

        viewed_on_xpath = './/div[contains(@class,"artdeco-entity-lockup__caption ember-view")]'

        viewer_elements: list[WebElement] = []
        previous_viewer_count = -1

        while True:  # Keep looping until we find a viewed on date out of range
            # Get Each Viewer within the last day (or time of dm run via database log)
            try:
                viewer_elements = get_elements_as_list_wait_stale(wait,
                                                                  '//ul[@aria-label="List of Entities"]//a[contains(@href,"linkedin.com/in") and not(contains(@aria-label,"Update"))]',
                                                                  "Finding Profile Viewers")
            except (TimeoutException, StaleElementReferenceException) as e:
                # An empty analytics page never resolves the wait — find_elements returns [] and the
                # helper polls to a timeout. Nobody viewed the profile (or the list didn't render) is
                # nothing to do, not a task failure, so it must not page the error cron.
                log_warning(f"Could not read the profile viewers list — ending the walk with "
                            f"{len(viewer_elements)} viewer(s)",
                            exc=e, user_id=user_id, task_name="automate_profile_viewer_engagement")
                break

            # myprint(f"Viewers count: {len(viewer_elements)}")

            if len(viewer_elements) > 0:
                # The list re-renders under us as we scroll it, so an unreadable last row means we
                # can't tell whether to keep walking — stop with what we have rather than failing.
                try:
                    # Get the last viewer
                    last_viewer = viewer_elements[-1]
                    # Extract the viewer's name
                    name_element = last_viewer.find_element(By.XPATH,
                                                            './/div[contains(@class,"artdeco-entity-lockup__title")]/span/span[1]')
                    if name_element:
                        last_viewer_name = getText(name_element)
                        # myprint(f"Last Viewer Name: {last_viewer_name}")
                    else:
                        last_viewer_name = random.choice(["John", "Jane"]) + " Doe"
                        myprint("Could not find name of last viewer")

                    last_viewed_on_element = last_viewer.find_element(By.XPATH, viewed_on_xpath)
                except (StaleElementReferenceException, NoSuchElementException) as e:
                    log_warning("Could not read the last profile viewer row — stopping the walk",
                                exc=e, user_id=user_id, task_name="automate_profile_viewer_engagement")
                    break

                if last_viewed_on_element:
                    # An unparseable caption raises out of convert_viewed_on_to_date — that is the
                    # same "we can't tell whether to keep walking" case as an unreadable row, so it
                    # ends the walk instead of failing the task through the outer handler.
                    try:
                        last_viewed_on = getText(last_viewed_on_element).strip()
                        # Convert viewed on to date
                        last_viewed_date = convert_viewed_on_to_date(last_viewed_on)
                    except (ValueError, TypeError, AttributeError,
                            StaleElementReferenceException) as e:
                        log_warning("Could not read the last profile viewer's viewed-on date — "
                                    "stopping the walk", exc=e, user_id=user_id,
                                    task_name="automate_profile_viewer_engagement")
                        break

                    # if the last viewed on date is Greater than 24 hours break the while loop
                    if (datetime.now() - last_viewed_date).days > 1:
                        # myprint("Last viewed on date is more than 24 hours ago")
                        break  # Break the while loop
                else:
                    myprint(f"Could not find viewed on element for {last_viewer_name}")

                # The list only grows by scrolling. When a scroll adds nothing there is no more to
                # walk, and every row we have is still in range — without this the loop would spin
                # forever on an account whose viewers all fit on one screen.
                if len(viewer_elements) <= previous_viewer_count:
                    myprint("Profile viewers list stopped growing. Ending the walk...")
                    break
                previous_viewer_count = len(viewer_elements)

                # Scroll down to get more elements
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(2)

            else:
                break  # Break the while loop

        # myprint(f"Viewers: {str(viewer_elements)}")
        result = f"Profile Viewer DMs Started. Found {len(viewer_elements)} viewers"
        myprint(f"Final Viewers count: {len(viewer_elements)}")

        # Filter the viewers by date within the last day, per row. The walk stops ON an
        # out-of-range viewer, so that row is always in this list — an all-or-nothing filter that
        # threw on one unreadable caption would leave it in and DM someone who viewed weeks ago.
        # A row whose date we cannot read is dropped for the same reason.
        in_range_viewers: list[WebElement] = []
        for e in viewer_elements:
            try:
                viewed_date = convert_viewed_on_to_date(
                    getText(e.find_element(By.XPATH, viewed_on_xpath)))
            except (ValueError, TypeError, AttributeError, StaleElementReferenceException,
                    NoSuchElementException) as ex:
                log_warning("Skipping profile viewer with an unreadable viewed-on date", exc=ex,
                            user_id=user_id, task_name="automate_profile_viewer_engagement")
                continue
            if (datetime.now() - viewed_date).days <= 1:
                in_range_viewers.append(e)
        viewer_elements = in_range_viewers

        myprint(f"Filtered Viewers count: {len(viewer_elements)}")

        current_tab = driver.current_window_handle
        handles = driver.window_handles

        # Get all the viewer names and urls into a dict so that elements don't go stale. One
        # unreadable row must not lose the whole batch — the list re-renders while we scroll it.
        viewer_data: dict[str, str] = {}
        for e in viewer_elements:
            try:
                viewer_name = getText(
                    e.find_element(By.XPATH,
                                   './/div[contains(@class,"artdeco-entity-lockup__title")]/span/span[1]'))
                viewer_url = e.get_attribute('href')
            except (StaleElementReferenceException, NoSuchElementException) as ex:
                log_warning("Skipping unreadable profile viewer row", exc=ex, user_id=user_id,
                            task_name="automate_profile_viewer_engagement")
                continue
            if viewer_url:
                viewer_data[viewer_name] = viewer_url

        # Get the viewed data from each element and filter by a day ago or specific date
        for viewer_name, viewer_url in viewer_data.items():
            # Switch back to tab
            driver.switch_to.window(current_tab)

            myprint(f"Viewer Name: {viewer_name}")
            myprint(f"Viewer URL: {viewer_url}")

            # Wait for the new window or tab
            driver.switch_to.new_window('tab')
            wait.until(EC.new_window_is_opened(handles))

            # Switch to viewer_url
            driver.get(viewer_url)

            # Engage with the viewer
            kwargs = {'user_id': get_user_id(my_profile.email),
                      'viewer_url': viewer_url,
                      'viewer_name': viewer_name}
            engage_with_profile_viewer.apply_async(kwargs=kwargs)

            # Close tab when done
            close_tab(driver)

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
                myprint(f"Loop duration reached. Stopping {current_function_name} task...")
            else:
                # Change the value of the loop_for_duration parameter
                kwargs['loop_for_duration'] = new_loop_for_duration
                # Add our function call back to the task queue
                myprint(f"Adding {current_function_name} back to queue for {future_forward} seconds in the future...")
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
    myprint("Starting Profile Viewer Engagement")

    result = "Profile Viewer Engagement Started"
    engagement_successful = False

    # Check if we already engaged with this viewer today
    if has_engaged_url_with_x_days(user_id, viewer_url, 1):
        myprint(f"Already engaged with {viewer_name} today. Skipping...")
        result = f"Already engaged with {viewer_name} today. Skipping..."
    else:

        try:
            driver, wait, user_email, my_profile = get_current_profile(user_id=user_id,
                                                                       session_name="Profile Viewer Engagement")
        except Exception as e:
            log_error("Error while getting profile for profile viewer engagement", exc=e, user_id=user_id, task_name="engage_with_profile_viewer")
            return f"Failed to start profile viewer engagement: {e}"

        try:

            myprint(f"Engaging from: {my_profile.full_name} to: {viewer_name}")

            if viewer_url != driver.current_url:
                # Switch to viewer_url
                driver.get(viewer_url)

            profile_data = get_linkedin_profile_from_url(driver, wait, viewer_url)
            if profile_data:
                profile = LinkedInProfile(**profile_data)
                # message = profile.generate_personalized_message()
                # myprint(message)

                if profile.is_1st_connection:
                    myprint("We Are 1st Connections")
                    # engage with their content (
                    recent_activities = profile.recent_activities

                    myprint(f"Recent Activities Count: {len(recent_activities)}")

                    # Filter activities by posted date less than a week ago
                    recent_activities = [activity for activity in recent_activities if
                                         (datetime.now() - activity.posted).days <= 7]

                    myprint(f"Recent Activities Filtered (1 week) Count: {len(recent_activities)}")

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
                        myprint("No activities, unable to or already left comment")

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
                    myprint(f"Original Response: {response}")
                    refined_response = get_ai_message_refinement(response)
                    myprint(f"Refined Response: {refined_response}")

                    # Send connection request with this message
                    kwargs = {'user_id': get_user_id(my_profile.email),
                              'profile_url': str(profile.profile_url),
                              'message': refined_response}
                    invite_to_connect.apply_async(kwargs=kwargs)
                    result = f"Profile Viewer Engagement Completed. Sent Connection Request to {viewer_name}"
                    engagement_successful = True
            else:
                myprint(f"Failed to get profile data for {viewer_name}")
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


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True}, reject_on_worker_lost=True,
                  rate_limit='2/m', queue='se_outreach')
def clean_stale_invites(self, user_id: int):
    """Cleans up stale invites that the user has sent. Not yet implemented — no-op stub.

    Emits an explicit log line and returns a descriptive marker so operators can tell an
    intentional stub run apart from a real cleanup run in logs/metrics."""

    log_info("clean_stale_invites is a no-op stub (not yet implemented) — skipping",
             user_id=user_id, task_name="clean_stale_invites", action_type="invite")

    return {"status": "not_implemented", "cleaned": 0, "user_id": user_id}


def send_dm_now(user_id: int, profile_url: str, message: str) -> bool:
    """Core DM send: open the profile, type + send a DM (must be a 1st-degree connection), log the
    result. Returns True on success. Shared by send_private_dm (trigger-driven) and send_scheduled_dm
    (issue #306 scheduler) so both use the same send + logging path."""
    user_email, user_password = get_user_password_pair_by_id(user_id)

    driver, wait = get_driver_wait_pair(session_name='Private DM', user_id=user_id)

    login_to_linkedin(driver, wait, user_email, user_password)

    # Open the profile URL
    driver.get(profile_url)

    dm_sent = False

    myprint("Sending DM: " + message)

    try:

        # Click on message button
        click_element_wait_retry(driver, wait, '//main//button[contains(@aria-label,"Message")]',
                                 "Finding Message Button", max_retry=1, use_action_chain=True)

        # Find the message box
        message_box = get_element_wait_retry(driver, wait, '//div[contains(@class,"contenteditable")]//p',
                                             'Finding Message Box', max_try=1, )

        # Select All (Must be done this way. Clear command does not work)
        message_box.send_keys(Keys.CONTROL + "a")
        # Delete what is selected
        message_box.send_keys(Keys.DELETE)

        # Find the message box (again)
        # message_box = driver.switch_to.active_element
        message_box = get_element_wait_retry(driver, wait, '//div[contains(@class,"contenteditable")]//p',
                                             'Finding Message Box', max_try=1, )

        # Type the message into the box
        simulate_typing(driver, message_box, message)

        # Sleep so send button can become active
        time.sleep(2)

        # Click the send button
        click_element_wait_retry(driver, wait, "//button[contains(@class,'msg-form__send-button')]",
                                 "Finding Send Button", max_retry=1, use_action_chain=True)

        dm_sent = True

    except Exception as e:
        myprint(f"DM send failed. Error: {str(e)}")

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
    """ Send dm message to a profile. Must be a 1st connection"""
    dm_sent = send_dm_now(user_id, profile_url, message)
    result = "DM Sent Successfully" if dm_sent else "DM Failed"
    myprint(result)
    return result


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'keys': ['dm_id']},
                  reject_on_worker_lost=True, rate_limit='2/m', queue='se_outreach')
def send_scheduled_dm(self, dm_id: int):
    """Send a scheduled 1:1 DM (issue #306). Enforces the per-day DM cap at send time (defers back
    to 'approved' for the next scan when the cap is hit) and updates the scheduled_dms status."""
    from cqc_lem.utilities.db import (get_scheduled_dm, update_scheduled_dm_status,
                                      count_dms_sent_today, ScheduledDmStatus)
    dm = get_scheduled_dm(dm_id)
    if not dm or dm["status"] not in (ScheduledDmStatus.APPROVED, ScheduledDmStatus.SCHEDULED):
        return f"Scheduled DM {dm_id} not sendable (status={dm['status'] if dm else 'missing'})"

    user_id = dm["user_id"]
    prefs = get_engagement_preferences(user_id)
    if remaining_actions(user_id, ACTION_DM, int(prefs.get("max_dms_per_day") or 0),
                         count_dms_sent_today(user_id),
                         caps=engagement_caps_from_prefs(prefs)) <= 0:
        myprint(f"send_scheduled_dm: daily DM budget spent for user {user_id}; deferring DM {dm_id}")
        update_scheduled_dm_status(dm_id, ScheduledDmStatus.APPROVED)  # retry on the next scan
        return f"Scheduled DM {dm_id} deferred (daily DM cap reached)"

    dm_sent = send_dm_now(user_id, dm["recipient_profile_url"], dm["message"])
    update_scheduled_dm_status(dm_id, ScheduledDmStatus.SENT if dm_sent else ScheduledDmStatus.FAILED)
    return f"Scheduled DM {dm_id} -> {'sent' if dm_sent else 'failed'}"


def _reply_to_person_on_post(driver, wait, post_url: str, person_profile_url: str, text: str,
                             user_id: int = None) -> bool:
    """Post `text` as a reply UNDER a specific person's comment on `post_url` — the delivery half of
    an approved hot-lead response (issue #483). Reuses the #478 comment-thread helpers: a tall
    viewport + scrolling is what actually makes comments lazy-render on a long post."""
    slug = _profile_slug(person_profile_url)
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
    on a signal a human APPROVED — nothing here can auto-respond to a lead on its own."""
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


# The connection-degree badge on a profile page. Only used to ABORT a pointless invite, so a
# selector that stops matching costs nothing but the old behaviour (issue #623).
_PROFILE_DEGREE_LOCATORS = [
    (By.CSS_SELECTOR, "main span.dist-value"),
    (By.CSS_SELECTOR, "main span.distance-badge"),
    (By.XPATH, "//main//span[contains(@class,'distance-badge')]"),
]


def _profile_is_first_degree(driver) -> bool:
    """True only when the profile page SAYS 1st degree. Fails open (False) on any read problem —
    a missed badge just means we try the invite, which is the old behaviour."""
    try:
        for by, selector in _PROFILE_DEGREE_LOCATORS:
            for element in driver.find_elements(by, selector):
                if is_first_degree(element.text or ""):
                    return True
    except Exception as e:
        log_warning("Could not read the connection-degree badge; attempting the invite anyway",
                    exc=e, action_type="invite_connect")
    return False


def _click_connect_affordance(driver, wait, user_id: int) -> bool:
    """Click Connect on an open profile — the direct button first, else the one inside the
    More-actions overflow. False when NEITHER is there, which is an ordinary outcome (an invite is
    already pending, LinkedIn only offers Follow/Message on that profile, or the SDUI selector
    drifted) and is why the miss is a WARNING and not an error (issue #571)."""
    try:
        click_element_wait_retry(driver, wait, '//main//button[contains(@aria-label, "Invite ")]',
                                 "Finding Connect Button", max_retry=1, use_action_chain=True)
        myprint("Found Connect Button and clicked it")
        return True
    except Exception:
        pass  # No direct Connect button — LinkedIn buries it in the More menu on many profiles.

    try:
        click_element_wait_retry(driver, wait,
                                 '//main//button[contains(@aria-label,"More actions")]',
                                 "Finding More Button", max_retry=1, use_action_chain=True)
        myprint("Found More Button and clicked it")

        click_element_wait_retry(driver, wait, '//main//div[contains(@aria-label,"connect")]',
                                 "Finding Connect Button", max_retry=1, use_action_chain=True)
        myprint("Found Connect Button and clicked it")
        return True
    except Exception as e:
        log_warning("No Connect option on this profile (direct button and More menu both missed)",
                    exc=e, user_id=user_id, action_type="invite_connect")
        return False


def _add_connect_note(driver, wait, message: str, user_id: int) -> bool:
    """Type a personalized note into an OPEN Connect dialog. Best-effort by design (issue #573):
    LinkedIn hides the note affordance once a free account's personalized-invite quota is spent, and
    a note that can't be attached must not cost us the invite — the caller sends it bare instead. So
    every miss here is a WARNING, and the note is stripped of non-BMP characters first because
    ChromeDriver's send_keys raises on the emoji an AI-written note routinely carries."""
    try:
        click_element_wait_retry(driver, wait, '//button[contains(@aria-label,"Add a note")]',
                                 "Finding Add a Note Button", max_retry=1, use_action_chain=True)

        message_box = click_element_wait_retry(driver, wait, '//textarea[@id="custom-message"]',
                                               "Finding Message Box", max_retry=1, use_action_chain=True)
        message_box.clear()

        for _ in range(3):
            if len(message) > CONNECT_NOTE_MAX_CHARS:
                message = get_ai_message_refinement(message, CONNECT_NOTE_MAX_CHARS)
            else:
                break

        message_box.send_keys(_strip_non_bmp(message)[:CONNECT_NOTE_MAX_CHARS])

        # The Send button only enables once the textarea has registered input.
        time.sleep(2)
        myprint("Added note to the connection request")
        return True
    except Exception as e:
        log_warning("Could not attach a note to the connection request; sending it without one",
                    exc=e, user_id=user_id, action_type="invite_connect")
        return False


# The Send button's aria-label differs between the bare dialog and the one carrying a note, and a
# note attempt can leave the dialog in either state — so both are tried, preferred label first.
_SEND_INVITE_XPATHS = ('//button[contains(@aria-label,"Send invitation")]',
                       '//button[contains(@aria-label,"Send without a note")]')


def _submit_connect_invite(driver, wait, user_id: int, with_note: bool) -> bool:
    """Click Send on the open Connect dialog. False only when NEITHER Send affordance is clickable,
    which loses the invite outright — that one stays an error (issue #573)."""
    xpaths = _SEND_INVITE_XPATHS if with_note else tuple(reversed(_SEND_INVITE_XPATHS))
    last_error: Exception = None
    for xpath in xpaths:
        try:
            click_element_wait_retry(driver, wait, xpath, "Finding Send Connection Button",
                                     max_retry=1, use_action_chain=True)
            myprint("Found Send Connection Button and clicked it")
            return True
        except Exception as e:
            last_error = e  # wrong label for this dialog state — try the other one

    # exc= is what turns this into a fingerprinted PostHog issue (the loop that filed #573); an
    # exc-less log_error only reaches Logs, so the one failure we deliberately keep as an error
    # would never page anyone.
    log_error("Failed to send the connection request (no Send button on the open Connect dialog)",
              exc=last_error, user_id=user_id, action_type="invite_connect")
    return False


def invite_to_connect_now(user_id: int, profile_url: str, message: str = None) -> "tuple[bool, str]":
    """Core connect-invite send: open the profile, click Connect (+ optional note), log the result.
    Returns (sent, result_message) — the message is the failure reason when `sent` is False, which
    the proactive flow stores on the request row (issue #623). Shared by invite_to_connect (reactive
    profile-viewer flow) and send_connection_request (issue #398 approval-gated proactive flow) so
    both use the same send + log path (mirrors send_dm_now). Re-raises LinkedInRateLimited when the
    kill-switch / 429 breaker is open so callers can defer rather than record a false failure."""
    user_email, user_password = get_user_password_pair_by_id(user_id)

    driver, wait = get_driver_wait_pair(session_name='Invite to Connect', user_id=user_id)

    invite_sent = False

    try:

        login_to_linkedin(driver, wait, user_email, user_password)

        if profile_url != driver.current_url:
            # Open the profile URL
            driver.get(profile_url)

        myprint(f"Inviting to connect: {profile_url}")

        # Already connected? There is no Connect button to find — bail with a reason instead of
        # burning a session hunting for one and recording an opaque failure.
        if _profile_is_first_degree(driver):
            log_info("Skipping invite: already a 1st-degree connection", user_id=user_id,
                     action_type="invite_connect")
            insert_new_log(user_id=user_id, action_type=LogActionType.ENGAGED,
                           result=LogResultType.FAILURE, post_url=profile_url,
                           message=ALREADY_CONNECTED_MESSAGE)
            return False, ALREADY_CONNECTED_MESSAGE

        # Locate the connect button. With no Connect dialog open the note/send steps below can only
        # fail, and their errors would bury the real reason — so stop here with a named one instead.
        if not _click_connect_affordance(driver, wait, user_id):
            insert_new_log(user_id=user_id, action_type=LogActionType.ENGAGED,
                           result=LogResultType.FAILURE, post_url=profile_url,
                           message=NO_CONNECT_BUTTON_MESSAGE)
            return False, NO_CONNECT_BUTTON_MESSAGE

        # The note is an optional extra on an invite we already decided to send — a missing note
        # affordance must not abandon an open Connect dialog, so the invite goes out bare (#573).
        noted = bool(message) and _add_connect_note(driver, wait, message, user_id)

        result = (CONNECTION_REQUEST_SENT_MESSAGE
                  if _submit_connect_invite(driver, wait, user_id, with_note=noted)
                  else INVITE_NOT_SENT_MESSAGE)
    except LinkedInRateLimited:
        # Kill-switch / 429 breaker is open — let the caller defer instead of logging a false failure.
        raise
    except Exception as e:
        log_error("Error while inviting to connect", exc=e, user_id=user_id, action_type="invite_connect")
        result = f"Error while inviting to connect: {e}"
        insert_new_log(user_id=user_id, action_type=LogActionType.ENGAGED,
                       result=LogResultType.FAILURE, post_url=profile_url, message=str(e))
    else:
        invite_sent = result == CONNECTION_REQUEST_SENT_MESSAGE
        insert_new_log(user_id=user_id, action_type=LogActionType.ENGAGED,
                       result=LogResultType.SUCCESS if invite_sent else LogResultType.FAILURE,
                       post_url=profile_url, message=result)
        if invite_sent:
            record_action(user_id, ACTION_INVITE)  # account-level governor (issue #626)
    finally:
        quit_gracefully(driver)  # Close the driver

    return invite_sent, result


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'keys': ['user_id', 'profile_url']},
                  reject_on_worker_lost=True, rate_limit='1/m', queue='se_outreach')
def invite_to_connect(self, user_id: int, profile_url: str, message: str = None):
    """Send a LinkedIn connection request (reactive profile-viewer flow). Thin wrapper over
    invite_to_connect_now; a throttle / kill-switch defers silently."""
    try:
        sent, reason = invite_to_connect_now(user_id, profile_url, message)
    except LinkedInRateLimited as e:
        myprint(f"invite_to_connect deferred (throttled): {e}")
        return "Invitation deferred (LinkedIn throttled)"
    if sent:
        return CONNECTION_REQUEST_SENT_MESSAGE
    log_warning(f"Connection request failed: {reason}", user_id=user_id, action_type="invite_connect")
    return f"Connection Request Failed: {reason}"


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'keys': ['request_id']},
                  reject_on_worker_lost=True, rate_limit='1/m', queue='se_outreach')
def send_connection_request(self, request_id: int):
    """Send an approved proactive connection request (issue #398). Enforces the per-day invite cap at
    send time (defers back to 'approved' for the next scan when the cap is hit or LinkedIn is
    throttled) and updates the connection_requests status. Reuses invite_to_connect_now, so it
    honors the rate-limit / kill-switch."""
    from cqc_lem.utilities.db import (get_connection_request, update_connection_request_status,
                                      count_invites_sent_today, ConnectionRequestStatus)
    req = get_connection_request(request_id)
    if not req or req["status"] not in (ConnectionRequestStatus.APPROVED, ConnectionRequestStatus.SENDING):
        return f"Connection request {request_id} not sendable (status={req['status'] if req else 'missing'})"

    user_id = req["user_id"]
    prefs = get_engagement_preferences(user_id)
    if count_invites_sent_today(user_id) >= int(prefs.get("max_invites_per_day") or 0):
        myprint(f"send_connection_request: daily invite cap reached for user {user_id}; deferring {request_id}")
        update_connection_request_status(request_id, ConnectionRequestStatus.APPROVED)  # retry on next scan
        return f"Connection request {request_id} deferred (daily invite cap reached)"

    try:
        sent, reason = invite_to_connect_now(user_id, req["recipient_profile_url"], req["message"])
    except LinkedInRateLimited as e:
        myprint(f"send_connection_request: throttled, deferring {request_id}: {e}")
        update_connection_request_status(request_id, ConnectionRequestStatus.APPROVED)  # retry on next scan
        return f"Connection request {request_id} deferred (LinkedIn throttled)"
    if not sent:
        log_warning(f"Connection request {request_id} failed: {reason}", user_id=user_id,
                    action_type="invite_connect")
    update_connection_request_status(
        request_id, ConnectionRequestStatus.SENT if sent else ConnectionRequestStatus.FAILED,
        failure_reason=(None if sent else reason))
    return f"Connection request {request_id} -> {'sent' if sent else 'failed'}"


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


def _author_display_name(profile_url: str) -> str:
    """Readable name for an adjacent author from their /in/ slug (we don't scrape their profile just
    to write a note). 'jane-doe-1a2b3c' -> 'Jane Doe'."""
    slug = _profile_slug(profile_url)
    if not slug:
        return ""
    words = [w for w in slug.split("-") if w and not any(ch.isdigit() for ch in w)]
    return " ".join(w.capitalize() for w in words[:3])


def _harvest_post_commenters(driver, post_url: str, author_name: str, now: datetime,
                             limit: int = _MAX_ENGAGERS_PER_ADJACENT_POST) -> list:
    """Commenters on ONE post as connection-targeting signals. Reuses the SDUI comment-thread walker
    the reply sweep uses, so it survives the same DOM churn."""
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
    others' candidates."""
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
    spends tomorrow's cap the moment it opens."""
    cap = int(prefs.get("max_invites_per_day") or 0)
    remaining = cap - count_invites_sent_today(user_id) - count_open_connection_requests(user_id)
    ceiling = _MAX_NEW_CONNECT_TARGETS_PER_SCAN if max_new is None else int(max_new)
    return max(0, min(remaining, ceiling))


def _draft_connect_note(user_id: int, candidate: ScoredCandidate, topic: str = None) -> str:
    """Personalized connect note for one candidate: a grounded template (it names the actual shared
    context) refined into the user's voice. Falls back to the template if the LLM is unavailable —
    a missing note must never block the target."""
    base = default_connect_note(candidate, topic=topic)
    try:
        refined = (get_ai_message_refinement(base, character_limit=CONNECT_NOTE_LIMIT) or "").strip()
    except Exception as e:
        log_warning("Connect-note refinement failed", exc=e, user_id=user_id,
                    action_type="connection_targeting")
        return base
    return (refined or base)[:CONNECT_NOTE_LIMIT]


def _target_status_for_mode(mode: str, prefs: dict) -> "ConnectionRequestStatus":
    """'suggest' (default) ALWAYS files a draft needing human approval; 'auto_queue' defers to the
    user's #398 connection_request_mode. So enabling targeting alone can never send anything."""
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
    apply. Nothing is sent from here."""
    prefs = get_engagement_preferences(user_id)
    mode = str(prefs.get("connection_targeting_mode") or "suggest")
    if mode == "off":
        log_info("Connection targeting is off for this user", user_id=user_id,
                 task_name="scan_connection_candidates", action_type="connection_targeting")
        return f"Connection targeting off for user {user_id}"

    budget = _connect_target_budget(user_id, prefs, max_new)
    if budget <= 0:
        log_warning("Connection targeting filed nothing: no invite budget left (daily cap already "
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
        log_warning("Connection targeting filed nothing: nobody has engaged with this user's "
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
        log_warning("Connection targeting filed nothing: every candidate was already requested, "
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
    Returns '' when there's nothing to pre-draft — the operator can still edit before approving."""
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
    for the next run) rather than auto-firing it."""
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
    DM follow-up machinery; daily comment/DM caps defer a stage rather than fire it."""
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
    Each roster author is best-effort: one unreachable profile must not lose the others."""
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
            log_warning(f"Roster author {author_url} has no recent post to comment on — skipping",
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
    we're already connected to is dropped, since connecting is the only thing this funnel adds."""
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
    template comment going out under their name."""
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
    re-approval, and the daily comment/DM caps in process_outreach_funnel all still apply."""
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
        log_warning(f"Outreach funnel sourcing filed nothing: {open_targets} target(s) are already "
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
        log_warning("No active engagement-roster targets — the funnel can only source from post "
                    "engagers until a roster is configured", user_id=user_id,
                    task_name="scan_outreach_funnel_targets", action_type="outreach_funnel")
    prospects.extend(_funnel_prospects_from_engagers(user_id))

    if not prospects:
        log_warning("Outreach funnel sourcing filed nothing: the engagement roster produced no "
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

# Ordered fallback chain for the catch-up cards. LinkedIn's SDUI ships hashed classes, so we prefer
# data-view-name, then the semantic list, then any main-content list item that links to a profile.
# NOTE: needs a supervised live-grounding pass on a real account before broad enablement (see #478).
_CATCHUP_CARD_LOCATORS = [
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
    """Strip query/fragment/trailing slash so the same person can't enter the dedup ledger twice
    under two URL spellings (LinkedIn appends tracking params to catch-up links)."""
    if not url:
        return ""
    parsed = urlparse(url.strip())
    path = (parsed.path or "").rstrip("/")
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}{path}"
    return path


def _classify_catchup_moment(text: str) -> "str | None":
    """Map a catch-up card's text to a CatchupEventType value, or None when it isn't a moment we
    know how to congratulate (LinkedIn also renders suggestions/ads in this feed)."""
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


def _score_catchup_moment(moment: dict, prefs: dict) -> int:
    """Score = milestone value + ICP fit. The literal keyword check is free; the LLM relevance check
    only runs when the literal one missed AND the user configured include_topics, so scoring a day's
    feed costs at most a handful of lem-simple calls."""
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
    timeout on every non-person card LinkedIn mixes into the feed."""
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
    DM budget. Anything that doesn't look like a message (empty / a bare label) becomes ''."""
    cleaned = " ".join((text or "").replace("\n", " ").split()).strip()
    if len(cleaned) < 4 or _CATCHUP_CHROME_RE.fullmatch(cleaned):
        return ""
    return cleaned[:CATCHUP_MESSAGE_MAX_CHARS]


def _card_suggested_message(card: WebElement) -> str:
    """LinkedIn's suggested congratulations as rendered ON the card (a quick-reply chip), if any.
    Read-only — no clicking."""
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
    falls back to the per-milestone default."""
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
    (logged) rather than failing the run."""
    driver.get(CATCHUP_URL)
    time.sleep(random.uniform(3, 5))

    moments: List[dict] = []
    seen: set = set()
    for _ in range(3):  # the feed lazy-loads; a few scrolls cover a normal day's moments
        cards = find_all_first(driver, _CATCHUP_CARD_LOCATORS)
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

    if not moments:
        # DEBUG, not WARNING: an empty catch-up feed is a normal day, and the scan beat runs daily
        # per user — three of them inside the escalation window would re-emit at ERROR and file a
        # grouped $exception for a no-op. The `no_moments` status on the run report carries it.
        log_debug("Catch-up feed produced no moments", user_id=user_id, action_type="catchup")
    return moments


def _catchup_name_from_card(text: str, link) -> str:
    """The card's person name: the profile link's own text when LinkedIn renders it, else the first
    line of the card (which leads with the name)."""
    try:
        name = (getText(link) or "").strip().split("\n")[0]
    except (StaleElementReferenceException, NoSuchElementException):
        name = ""
    if not name:
        name = (text or "").strip().split(" ·")[0]
    return name[:255]


def _draft_catchup_message(user_id: int, moment: dict, my_profile: LinkedInProfile,
                           source: str = "linkedin") -> "str | None":
    """The congratulations to send for this moment.

    'linkedin' (default): LinkedIn's OWN pre-drafted response — the suggestion it renders on the card
    or pre-fills in its composer — falling back to the matching plain one-liner when the feed gave us
    none. No LLM call. 'ai': the user's DM template refined to their voice (returns None when they
    deactivated the template for that event type)."""
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
                                     CATCHUP_STATUS_AWAITING_APPROVAL})


def report_catchup_run(user_id: Optional[int], report: dict, task_name: str) -> None:
    """Ship ONE catch-up run report (issue #792) and log the same line locally. `user_id` is None for
    the fleet-wide beat dispatchers. Best-effort: a telemetry outage must never fail a scan that
    otherwise worked."""
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
    milestone is deduped on (person, event_type, period), and moments scoring below CATCHUP_MIN_SCORE
    are tombstoned as 'skipped' so we neither draft nor re-score them. The message itself is
    LinkedIn's own pre-drafted response unless the user opted into catchup_message_source='ai'.

    EVERY run reports (issue #792) — including the ones that draft nothing — with the per-stage
    funnel counts, because a quiet lane and a broken one are otherwise the same silence."""
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
        myprint(f"automate_catchup_touches deferred (throttled): {e}")
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
        myprint(f"automate_catchup_touches deferred mid-scrape (throttled): {e}")
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

    funnel = {"classified": 0, "enabled_type": 0, "excluded": 0, "duplicate": 0, "below_bar": 0}
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

    EVERY outcome reports (issue #792). A deferral puts the touch back to 'approved' and the drip
    re-dispatches it 20 minutes later, so without this the send phase's `dispatched` count climbs all
    day on a lane that has never delivered a single message — which IS the reported symptom."""
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

    try:
        sent = send_dm_now(user_id, touch["profile_url"], touch["message"])
    except LinkedInRateLimited as e:
        myprint(f"send_catchup_touch: throttled, deferring {touch_id}: {e}")
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
    only ever checks for a reply and is then stopped; it can never send an extra DM."""
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
    and never duplicates a target already in the funnel. Best-effort: never breaks the follow-up loop."""
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


def final_method(drivers: List[WebDriver]):
    global stop_all_thread
    stop_all_thread.set()  # Set the flag to stop other threads
    for driver in drivers: quit_gracefully(driver)  # Quit all the drivers
    myprint("All drivers stopped. Program has exited.")
    sys.exit(0)


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True}, reject_on_worker_lost=True,
                  rate_limit='1/m', queue='se_outreach')
def update_stale_profile(self, user_id: int):
    myprint(f"Updating Stale Profile. User ID: {user_id}")
    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Update Stale Profile")
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


def get_current_profile(user_id: int, session_name: str = "Get Current Profile",
                        measurement_only: bool = False, debug: bool = False) -> Tuple[
    WebDriver, WebDriverWait, str, LinkedInProfile]:
    """Update the profile of the user.

    `measurement_only` marks a read-only stat-capture session, which keeps running under the
    suppression tripwire's own pause (and only that one) so recovery stays measurable — see
    rate_limit.is_measurement_paused.

    `debug` requests the watchable Grid debug node (if free) for live inspection; it falls
    back to the normal pool when the node is busy or absent."""

    myprint("Getting Updated Profile")

    user_email, user_password = get_user_password_pair_by_id(user_id)

    driver, wait = get_driver_wait_pair(session_name=session_name, user_id=user_id, debug=debug)

    # Login first — a failure here (e.g. HTTP 429 rate-limit, expired cookie) is fatal
    # for this run; abort cleanly so the caller backs off instead of hammering LinkedIn.
    try:
        login_to_linkedin(driver, wait, user_email, user_password,
                          measurement_only=measurement_only)
    except Exception as e:
        log_error("LinkedIn login failed (possibly rate-limited)", exc=e, user_id=user_id)
        quit_gracefully(driver)
        raise e

    # A live profile refresh can fail independently (auth-wall on the profile view,
    # transient DOM change) even when the feed is reachable. Don't let that abort the
    # whole task — fall back to the user's cached profile so commenting can proceed.
    try:
        my_profile = get_my_profile(driver, wait, user_email, user_password, user_id=user_id)
    except Exception as e:
        log_warning("Live profile refresh failed; falling back to cached profile", exc=e, user_id=user_id)
        my_profile = None

    if my_profile is None and user_id is not None:
        my_profile = load_profile_for_user(user_id)

    if my_profile is None:
        log_error("No profile available (live scrape failed and no cached profile)", user_id=user_id)
        quit_gracefully(driver)
        raise RuntimeError("Profile unavailable: live scrape failed and no cached profile to fall back on")

    return driver, wait, user_email, my_profile


if __name__ == "__main__":
    # Create the driver
    # driver = create_driver()
    # wait = get_driver_wait(driver)
    # test_already_commented(driver, wait)

    # test_ai_responses()
    # generate_ai_response_test
    # test_dates()
    # test_linked_in_profile()
    # test_get_linkedin_profile_from_url()
    # test_describe_profile()
    # test_describe_summarize_interesting_activity()
    # test_post_comment()
    # test_send_dm()
    # test_invite_to_connect()
    # exit(0)

    # start_process()
    # myprint("Process finished")
    pass


def _affiliate_disclosure_gate(user_id: int, post_id: int, content: str,
                               first_comment_links: Optional[list] = None) -> Optional[str]:
    """Refuse to publish affiliate promotion that carries no FTC disclosure (issue #737).

    Returns None when the post may publish, or the task's return string when it may not. A blocked
    post is flagged ERROR rather than dropped: it is the author's own material and a human has to
    decide whether to add the disclosure or remove the link — silently publishing an undisclosed
    paid endorsement is the one outcome that is not available.

    Non-affiliate posts (virtually all of them) never touch a disclosure requirement, and any
    unexpected failure here publishes: this is a compliance check on a rare shape of post, not a new
    way for the whole posting path to break."""
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
        from cqc_lem.utilities.observability import (AFFILIATE_DISCLOSURE_BLOCKED,
                                                     track_affiliate_event)
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
    myprint(f"Post To LinkedIn | Task ID: {task_id}")

    # Skip if already posted — prevents duplicate posts when the task is re-queued
    if get_post_status(post_id) == PostStatus.POSTED.value:
        myprint(f"Post {post_id} already posted. Skipping duplicate execution.")
        return f"Post {post_id} already posted — skipped"

    # Login and publish post to LinkedIn
    user_email, user_password = get_user_password_pair_by_id(user_id)
    myprint(f"Posting to LinkedIn as user: {user_email}")

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

    myprint(f"Posting to LinkedIn: {content}")

    post_type = get_post_type(post_id)
    myprint(f"Post type: {post_type}")

    if post_type in (PostType.CAROUSEL, PostType.DOCUMENT):
        slides = get_carousel_slides(post_id)
        label = "Document" if post_type == PostType.DOCUMENT else "Carousel"
        myprint(f"{label} slides ({len(slides)}): {slides}")
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
            myprint(f"Adding to Post | Video URL: {video_url}")
        urn = share_on_linkedin(user_id, content, video_url)
    else:
        urn = share_on_linkedin(user_id, content)

    if urn:
        post_url = f"https://www.linkedin.com/feed/update/{urn}/"
        myprint(f"Successfully created post using /posts API endpoint: {post_url}")

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
            myprint(f"purge_post_assets failed for post_id={post_id}: {e}")

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


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': False}, reject_on_worker_lost=True,
                  rate_limit='4/m', queue='se_outreach')
def automate_invites_to_company_page_for_user(self, user_id: int):
    """Send this user's paced daily drip of company-page invites (issue #732).

    The budget is decided BEFORE a browser session is opened: on most days the allowance is zero
    (rest day, budget already spent, single-digit cap already reached) and a Chrome slot spent to
    discover that is a slot an engagement lane needed. A paused account stands down here too — page
    invites are discretionary amplification, never a response owed to someone."""
    task_name = "automate_invites_to_company_page_for_user"

    if is_automation_paused():
        report = {"status": INVITE_STATUS_PAUSED}
        log_info("Company page invites skipped — automation paused", user_id=user_id,
                 task_name=task_name, action_type="company_invite")
        track_company_page_invite_run(user_id, report)
        return "Company page invites skipped — automation paused"

    plan = plan_daily_invites(user_id)
    if plan["allowance"] <= 0:
        report = {"status": plan["status"], "cap": plan["cap"], "sent_today": plan["sent_today"]}
        log_info(f"Company page invites skipped — {plan['status']} "
                 f"(cap {plan['cap']}, sent today {plan['sent_today']})",
                 user_id=user_id, task_name=task_name, action_type="company_invite")
        track_company_page_invite_run(user_id, report)
        return f"No company page invites to send ({plan['status']})"

    # Session acquisition is INSIDE the reporting path: a run that could not start a browser is the
    # one failure mode `company_page_invite_run` exists to make visible, and letting the exception
    # escape here would emit nothing at all — indistinguishable from a day paced down to zero.
    try:
        driver, wait = get_driver_wait_pair(session_name='Company Page Invites', user_id=user_id)
    except Exception as e:
        log_error("Could not start a browser session for company page invites", exc=e,
                  user_id=user_id, task_name=task_name, action_type="company_invite")
        track_company_page_invite_run(user_id, {"status": INVITE_STATUS_SESSION_FAILED,
                                                "cap": plan["cap"], "sent_today": plan["sent_today"]})
        return "Company page invites skipped — browser session unavailable"

    try:
        report = automate_invitations(driver, wait, user_id, plan=plan)
    except Exception as e:
        log_error("Error while inviting to company page", exc=e, user_id=user_id, task_name=task_name, action_type="company_invite")
        report = {"status": INVITE_STATUS_FAILED, "cap": plan["cap"], "sent_today": plan["sent_today"]}
    finally:
        quit_gracefully(driver)

    track_company_page_invite_run(user_id, report)
    result = (f"Invited {report.get('invites_sent') or 0} people to the company page "
              f"({report.get('status')}).")
    log_info(result, user_id=user_id, task_name=task_name, action_type="company_invite")
    return result
