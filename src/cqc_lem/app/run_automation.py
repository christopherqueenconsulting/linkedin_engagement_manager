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
from typing import List, Tuple
from urllib.parse import urlparse

from celery_once import QueueOnce
from cqc_lem.app.my_celery import app as shared_task
from cqc_lem.utilities.ai.content_framework import select_blueprint
from cqc_lem.utilities.ai.ai_helper import generate_ai_response, get_ai_message_refinement, summarize_recent_activity, \
    ai_check_message_history, post_is_relevant, generate_newsletter_edition, generate_group_post, \
    generate_thread_reply, generate_seed_comment, choose_post_reaction, get_or_create_profile_synthesis, \
    synthesize_profile
from cqc_lem.utilities.ai.content_alignment import humanize_text, split_link_for_first_comment, \
    append_link_to_comment
from cqc_lem.utilities.date import convert_viewed_on_to_date
from cqc_lem.utilities.db import get_user_password_pair_by_id, get_user_id, insert_new_log, LogActionType, \
    CONNECTION_REQUEST_SENT_MESSAGE, \
    get_engagement_preferences, count_comments_today, get_recent_engagers, upsert_engager, \
    get_newsletter_settings, mark_newsletter_published, record_newsletter_subscriber_stat, \
    get_newsletter_edition, mark_edition_published, mark_edition_failed, \
    upsert_user_group, get_enabled_group_ids, record_post_stats, get_recent_posted_post_ids, \
    get_lead_magnet_settings, has_received_lead_magnet, record_lead_magnet_sent, \
    LogResultType, has_user_commented_on_post_url, get_post_url_from_log_for_user, get_post_message_from_log_for_user, \
    claim_post_for_comment, mark_post_commented, mark_post_reacted, release_post_claim, has_commented_post, \
    has_engaged_url_with_x_days, get_post_content, get_post_video_url, update_db_post_status, PostStatus, PostType, \
    update_db_post_content, update_db_post_first_comment_link, get_post_first_comment_link, \
    get_dm_history_for_profile, get_post_status, get_user_blog_url, get_post_type, get_carousel_slides, \
    get_dm_template, enqueue_followup, get_due_followups, mark_followup, stop_followups_for_profile, \
    set_profile_synthesis, get_duplicate_comment_posts, count_dms_sent_today, \
    get_approved_outreach_targets, update_outreach_target, update_outreach_target_status, \
    OutreachStage, OutreachStatus
from cqc_lem.utilities.linkedin.company_page_inviter import automate_invitations
from cqc_lem.utilities.linkedin.helper import login_to_linkedin, get_my_profile, get_linkedin_profile_from_url, \
    load_profile_for_user
from cqc_lem.utilities.linkedin.poster import share_on_linkedin, share_carousel_on_linkedin, \
    share_document_on_linkedin, comment_on_linkedin_post, object_urn_from_post_url
from cqc_lem.utilities.linkedin.profile import LinkedInProfile
from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited, _redis_client
from cqc_lem.utilities.linkedin_formatter import normalize_public_text
from cqc_lem.utilities.logger import myprint, log_error, log_info, log_warning
from cqc_lem.utilities.observability import track_post_outcome
from cqc_lem.utilities.selenium_util import click_element_wait_retry, \
    get_element_wait_retry, get_elements_as_list_wait_stale, getText, close_tab, get_driver_wait_pair, quit_gracefully, \
    wait_for_ajax, find_first, click_first, find_all_first
from dotenv import load_dotenv
from selenium.common import NoSuchElementException, JavascriptException
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

    try:
        # Find and click the "Sort by" dropdown
        click_element_wait_retry(driver, wait, '//button/hr',
                                 "Clicking Sort By Dropdown", use_action_chain=True)

        # time.sleep(1)
        wait_for_ajax(driver)

        # Select "Recent" from the dropdown
        click_element_wait_retry(driver, wait, '//div[contains(@class,"artdeco-dropdown")]/ul/li[2]',
                                 "Selecting Recent Option", max_retry=0, use_action_chain=True)

        wait_for_ajax(driver)
        time.sleep(3)  # Wait for the page to refresh with recent posts

        myprint("Feed Sorted By Recent Items First")

    except Exception as e:
        log_error("Error during feed sort", exc=e)

    # are_you_satisfied()


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


def simulate_reading_time(content):
    # Estimate reading time based on the number of words (average human reads 200-300 words per minute)
    words = len(content.split())
    read_time = words / 250 * 60  # Convert to seconds
    # Round to integer
    return round(read_time)


def simulate_thinking_time():
    # Random thinking time between 2 and 5 seconds
    return round(random.uniform(2, 5))


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

            myprint(f"Added Comment via Post Button")
            method_result = f"Added Comment via Post Button"

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
            myprint(f"Added Comment via return key. This might not have worked")
            method_result = f"Added Comment via return key. This might not have worked"

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
                    myprint(f"Added Post Reaction")
                    method_result += f" | Added Post Reaction"
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


def _card_for_textbox(driver, box):
    """Nearest ancestor of a post's text box that contains its Comment button — i.e. the post card."""
    return driver.execute_script(
        "let el=arguments[0],d=0;while(el&&d<15){"
        "if(el.querySelector&&el.querySelector(\"button[aria-label='Comment']\"))return el;"
        "el=el.parentElement;d++;}return null;", box)


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


def _feed_post_key(author: str, content: str) -> str:
    """Stable-ish dedup key for a feed post (no permalink/urn exists in the SDUI DOM anymore)."""
    digest = hashlib.sha1(f"{author}|{content[:200]}".encode("utf-8", "ignore")).hexdigest()[:20]
    return f"feedpost://{digest}"


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


def post_comment_inline(driver, wait, card, comment_text: str, user_id: int = None) -> bool:
    """Open the card's inline comment composer, type the comment, and submit via the composer's
    own Comment/Post button (the SDUI composer has no <form>). Returns True only if the comment
    actually lands (composer clears / appears in the list), not just because text was typed."""
    try:
        comment_text = _strip_non_bmp(comment_text)
        if not comment_text.strip():
            return False
        if click_first(driver, wait, [(By.CSS_SELECTOR, "button[aria-label='Comment']")],
                       "Open comment composer", parent_element=card, required=False, user_id=user_id) is None:
            return False
        time.sleep(random.uniform(1.5, 3))
        composer = find_first(driver, wait,
                              [(By.CSS_SELECTOR, "div[role='textbox'][aria-label*='creating comment']"),
                               (By.CSS_SELECTOR, "div[role='textbox']")],
                              "Comment composer", visible_only=True, required=False, user_id=user_id)
        if composer is None:
            return False
        composer.click()
        composer.send_keys(comment_text)
        time.sleep(random.uniform(1, 2))
        if not driver.execute_script(_SUBMIT_NEAR_COMPOSER_JS, composer):
            composer.send_keys(Keys.CONTROL, Keys.RETURN)  # fallback
        time.sleep(random.uniform(3, 5))
        return _composer_submitted(driver, composer, comment_text)
    except Exception as e:
        log_warning("Inline comment post failed", exc=e, action_type="comment", user_id=user_id)
        return False


def react_to_post_inline(driver, wait, card, post_content: str = None, comment_text: str = None,
                         user_id: int = None) -> bool:
    """Leave a single reaction on the card's post via the SDUI reaction fly-out.

    The 2026 SDUI reaction controls carry obfuscated hashed classes, so the stable anchors are
    aria-labels (verified live): the per-card 'Open reactions menu' trigger, the 'Reaction button
    state: ...' toggle, and the fly-out buttons whose aria-label is exactly the reaction name
    (Like / Celebrate / Support / Love / Insightful). The reaction itself is picked by a fast AI
    call scoped to the post + our comment, which self-falls-back to random. Returns True only if a
    reaction registered (the toggle no longer reads 'no reaction')."""
    try:
        state = find_first(driver, wait, [(By.CSS_SELECTOR, "button[aria-label^='Reaction button state']")],
                           "Reaction state", parent_element=card, required=False, visible_only=True, user_id=user_id)
        if state is not None and "no reaction" not in (state.get_attribute("aria-label") or "").lower():
            return False  # already reacted on this post

        reaction = choose_post_reaction(post_content, comment_text)
        # The reaction fly-out is hover-revealed off the card's primary Like/React toggle. Hover it
        # first (the menu opener is hidden until then), then click the opener.
        trigger = state or find_first(
            driver, wait,
            [(By.CSS_SELECTOR, "button[aria-label='React Like']"),
             (By.CSS_SELECTOR, "button[aria-label^='React']"),
             (By.CSS_SELECTOR, "button[aria-label='Like']")],
            "React toggle", parent_element=card, required=False, visible_only=True, user_id=user_id)
        if trigger is not None:
            try:
                ActionChains(driver).move_to_element(trigger).perform()
                time.sleep(random.uniform(0.6, 1.2))
            except Exception:
                pass
        opened = click_first(driver, wait, [(By.CSS_SELECTOR, "button[aria-label='Open reactions menu']")],
                             "Open reactions menu", parent_element=card, required=False, user_id=user_id)
        time.sleep(random.uniform(0.8, 1.6))
        # The fly-out can render just outside the card subtree, so match the reaction button globally
        # (only one menu is open at a time and click_first filters to visible elements).
        if opened is None or click_first(driver, wait,
                       [(By.CSS_SELECTOR, f"button[aria-label='{reaction}']"),
                        (By.CSS_SELECTOR, "button[aria-label='Like']")],
                       f"React {reaction}", required=False, user_id=user_id) is None:
            # Fly-out didn't open or the specific reaction wasn't found — fall back to clicking the
            # primary toggle directly, which leaves a default Like. Better a Like than no reaction.
            if trigger is None:
                return False
            driver.execute_script("arguments[0].click();", trigger)
        time.sleep(random.uniform(0.8, 1.5))
        wait_for_ajax(driver)
        after = find_first(driver, wait, [(By.CSS_SELECTOR, "button[aria-label^='Reaction button state']")],
                           "Reaction state (post-click)", parent_element=card, required=False,
                           visible_only=True, user_id=user_id)
        # Best-effort confirm: label flips away from 'no reaction'. If the toggle can't be re-read,
        # trust the click rather than false-negative.
        if after is not None and "no reaction" in (after.get_attribute("aria-label") or "").lower():
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


def _switch_feed_to_recent(driver, wait) -> None:
    """Best-effort: flip the feed sort from 'Top' to 'Recent' so golden-hour posts surface for
    commenting. Silent no-op if the 'Sort by' control isn't present."""
    try:
        btn = find_first(driver, wait, [(By.XPATH, "//button[contains(normalize-space(),'Sort by')]")],
                         "Feed sort control", required=False)
        if btn is None:
            return
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(random.uniform(1, 2))
        opt = find_first(driver, wait,
                         [(By.XPATH, "//*[self::button or @role='menuitem' or @role='menuitemradio'][normalize-space()='Recent']"),
                          (By.XPATH, "//*[normalize-space()='Recent']")],
                         "Recent sort option", required=False)
        if opt is not None:
            driver.execute_script("arguments[0].click();", opt)
            time.sleep(random.uniform(2, 3.5))
    except Exception as e:
        log_warning("Feed recent-sort failed", exc=e, action_type="scrape")


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


def comment_on_feed_inline(driver, wait, my_profile: LinkedInProfile, user_id: int,
                           max_posts: int = 10, deadline_ts: float = None, prefs: dict = None,
                           engagers: set = None) -> int:
    """Walk the SDUI feed and comment inline, prioritizing by a scoring matrix instead of DOM
    order: recency-dominant (golden hour), then relevance, reciprocity (people who engaged with
    us), and healthy activity. Applies targeting filters + per-day cap + a max-post-age gate.
    Returns the number of comments posted."""
    from selenium.common.exceptions import StaleElementReferenceException
    if prefs is None:
        prefs = get_engagement_preferences(user_id)
    if engagers is None:
        engagers = get_recent_engagers(user_id)
    daily_cap = prefs.get("max_comments_per_day") or 20
    remaining_today = max(0, daily_cap - count_comments_today(user_id))
    if remaining_today <= 0:
        myprint(f"Daily comment cap reached ({daily_cap}) — skipping")
        return 0
    max_posts = min(max_posts, remaining_today)
    max_age_min = (prefs.get("max_post_age_hours") or 24) * 60
    min_reactions = prefs.get("min_reactions") or 0
    # Stable VOICE synthesis (cached weekly, lazily created on first use) — the voice source for every
    # comment this run, in place of the bloated/volatile full profile JSON.
    profile_synthesis = get_or_create_profile_synthesis(user_id, my_profile)
    _switch_feed_to_recent(driver, wait)  # surface golden-hour posts; scoring still ranks them

    # Per-run comment ANGLE rotation from the shared framework core: each comment this run gets a
    # different archetype (Expander, Storyteller, Questioner, ...) so a day's comments never all
    # read from the same template. In-memory only — comments are too high-volume to justify a DB
    # shape history, and per-run rotation is what a reader of the same feed would notice.
    used_comment_shapes: list = []

    posted, seen, scrolls = 0, set(), 0
    # Reach funnel (surfaced to the user so they can tell when their targeting is too strict) and the
    # empty-filter fallback. examined = posts we looked at; hard = passed excludes/recency/min-reactions;
    # include = also matched the user's include topics/keywords/authors.
    examined_keys, hard_keys, include_keys = set(), set(), set()
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
            # Prefer the real /feed/update/ permalink so the logged post_url links correctly;
            # fall back to the synthetic hash for cards that expose no anchor.
            key = _post_permalink_from_card(card) or _feed_post_key(author, content)
            if key in seen:
                continue
            examined_keys.add(key)
            # Persistent, cross-run/worker dedup: skip anything already claimed or commented
            # (commented_posts ledger), plus historical SUCCESS comment logs, plus hard excludes.
            if (has_commented_post(user_id, key) or has_user_commented_on_post_url(user_id, key)
                    or not _passes_hard_excludes(content, author, prefs)):
                seen.add(key)
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
            candidates.append((_score_feed_post(meta, prefs, engagers), key, card, content, author, age))

        if candidates:
            candidates.sort(key=lambda c: c[0], reverse=True)
            score, key, card, content, author, age = candidates[0]
            seen.add(key)  # decided on this one either way
            # Include gate (may use the LLM topic classifier) on the chosen post. If the feed keeps
            # producing nothing that matches the user's include filters, RELAX to fallback for the rest
            # of the run — comment on the best feed post regardless of include (LinkedIn already curates
            # the feed to relevant content). Hard excludes / recency / min-reactions still applied.
            if not fallback_active and fallback_enabled and strict_misses >= _FEED_FALLBACK_AFTER_MISSES:
                fallback_active = True
                myprint("Feed targeting matched nothing — falling back to top feed posts for this run")
            if fallback_active:
                fallback_used = True
            elif post_matches_preferences(content, author, prefs):
                include_keys.add(key)
            else:
                strict_misses += 1
                continue
            # Atomically claim the post BEFORE spending an LLM call or commenting. If a prior/
            # concurrent run already holds it, we lose the race here and move on — at most one
            # comment per post per user, across the pre-post run, the golden-hour run, and retries.
            if not claim_post_for_comment(user_id, key):
                continue
            comment_blueprint = select_blueprint("comment", recent_formats=used_comment_shapes)
            comment_text = generate_ai_response(content, my_profile, None, prefs=prefs,
                                                profile_synthesis=profile_synthesis,
                                                blueprint=comment_blueprint)
            if comment_text and comment_blueprint.get("format"):
                used_comment_shapes.insert(0, comment_blueprint["format"])
            if comment_text:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", card)
                time.sleep(simulate_reading_time(content) / 2 + simulate_thinking_time())
                # React BEFORE submitting the comment: posting re-renders the card and staled the
                # element, so the old post-comment reaction attempt silently failed. Skip our OWN
                # posts. Non-fatal — a missed reaction never blocks the comment.
                if not _author_is_me(author, my_profile):
                    if react_to_post_inline(driver, wait, card, post_content=content,
                                            comment_text=comment_text, user_id=user_id):
                        mark_post_reacted(user_id, key)
                    else:
                        log_warning("Could not leave a reaction on post", user_id=user_id,
                                    action_type="comment")
                if post_comment_inline(driver, wait, card, comment_text, user_id=user_id):
                    mark_post_commented(user_id, key)
                    insert_new_log(user_id=user_id, action_type=LogActionType.COMMENT,
                                   result=LogResultType.SUCCESS, post_url=key, message=comment_text)
                    posted += 1
                    myprint(f"Commented on {author or 'a'}'s post "
                            f"(score {score:.2f}, age {'?' if age is None else str(age) + 'm'}) ({posted}/{max_posts})")
                    time.sleep(random.uniform(6, 14))  # human pacing between comments
                else:
                    release_post_claim(user_id, key)  # posting failed — let a later run retry
            else:
                release_post_claim(user_id, key)  # no comment generated — release the claim
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

    set_feed_funnel(user_id, {
        "examined": len(examined_keys),
        "passed_filters": len(hard_keys),      # cleared excludes + recency + min-reactions
        "matched_topics": len(include_keys),   # also matched include topics/keywords/authors
        "commented": posted,
        "fallback_used": fallback_used,
        "max_post_age_hours": prefs.get("max_post_age_hours") or 24,
        "min_reactions": min_reactions,
        "at": datetime.now().isoformat(),
    })
    myprint(f"Feed scan: examined {len(examined_keys)}, passed filters {len(hard_keys)}, "
            f"matched topics {len(include_keys)}, commented {posted}, fallback={fallback_used}")
    return posted


def _reply_to_comment_inline(driver, wait, comment_el, reply_text: str, user_id: int = None) -> bool:
    """Open a comment's inline reply box, type the reply, and submit (same SDUI pattern as
    post_comment_inline: role=textbox composer + Ctrl+Enter fallback). Returns True if posted."""
    try:
        if click_first(driver, wait, [(By.CSS_SELECTOR, "button[aria-label='Reply']")],
                       "Open reply box", parent_element=comment_el, required=False, user_id=user_id) is None:
            return False
        time.sleep(random.uniform(1.5, 3))
        composer = find_first(driver, wait,
                              [(By.CSS_SELECTOR, "div[role='textbox'][aria-label*='reply']"),
                               (By.CSS_SELECTOR, "div[role='textbox'][aria-label*='comment']"),
                               (By.CSS_SELECTOR, "div[role='textbox']")],
                              "Reply composer", visible_only=True, required=False, user_id=user_id)
        if composer is None:
            return False
        reply_text = _strip_non_bmp(reply_text)
        if not reply_text.strip():
            return False
        composer.click()
        composer.send_keys(reply_text)
        time.sleep(random.uniform(1, 2))
        if not driver.execute_script(_SUBMIT_NEAR_COMPOSER_JS, composer):
            composer.send_keys(Keys.CONTROL, Keys.RETURN)  # fallback
        time.sleep(random.uniform(3, 5))
        return _composer_submitted(driver, composer, reply_text)
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
    _lower = "translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')"
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


def _fill_and_publish_article(driver, wait, title: str, body: str, subtitle: str = None) -> "str | None":
    """Fill LinkedIn's article editor (title textarea + contenteditable body) and run the
    Next → Publish flow. On the publish dialog, best-effort fills the edition description with
    `subtitle`. Returns the published article URL, or None. Best-effort — the multi-step publish
    dialog varies, so this is validated on a supervised first real run."""
    title_el = find_first(driver, wait, [(By.CSS_SELECTOR, "textarea[placeholder='Title']")],
                          "Article title", required=False)
    body_el = find_first(driver, wait, [(By.CSS_SELECTOR, "div[role='textbox'][aria-label*='Article editor']"),
                                        (By.CSS_SELECTOR, "div[role='textbox']")],
                         "Article body", visible_only=True, required=False)
    if title_el is None or body_el is None:
        return None
    title_el.click()
    title_el.send_keys(_strip_non_bmp(title))
    time.sleep(random.uniform(1, 2))
    body_el.click()
    body_el.send_keys(_strip_non_bmp(body))
    time.sleep(random.uniform(2, 3))
    if click_first(driver, wait, [(By.XPATH, "//button[normalize-space()='Next']")],
                   "Article Next", required=False) is None:
        return None
    time.sleep(random.uniform(2, 4))
    _fill_edition_description(driver, wait, subtitle)   # best-effort; never blocks publishing
    if click_first(driver, wait, [(By.XPATH, "//button[normalize-space()='Publish']")],
                   "Article Publish", required=False) is None:
        return None
    time.sleep(random.uniform(4, 7))
    return driver.current_url


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
        url = _fill_and_publish_article(driver, wait, edition["title"], edition["body"],
                                        subtitle=edition.get("subtitle"))
        if url:
            mark_newsletter_published(user_id, url)
            myprint(f"Published newsletter edition for user {user_id}: {edition['title']}")
            return f"Published newsletter: {edition['title']}"
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
        url = _fill_and_publish_article(driver, wait, edition["title"], edition["body"],
                                        subtitle=edition.get("subtitle"))
        if url:
            mark_edition_published(edition_id, url)
            myprint(f"Published newsletter edition {edition_id} for user {user_id}: {edition['title']}")
            return f"Published newsletter edition: {edition['title']}"
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


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_content')
def auto_scrape_post_stats(self, user_id: int):
    """Capture reactions/comments/reposts/impressions/saves for each of the user's recent posts
    (feeds personalized post-time recommendations + the content feedback loop). Reuses the
    social-count extraction on each post's detail page, then on its analytics page for the
    signals the detail page never renders (saves, impressions)."""
    post_ids = get_recent_posted_post_ids(user_id)
    if not post_ids:
        return "No recent posts to scrape"
    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Post Stats")
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
            record_post_stats(user_id, pid, counts.get("reactions", 0), counts.get("comments", 0),
                              reposts=counts.get("reposts") or 0,
                              impressions=counts.get("impressions") or None,
                              saves=counts.get("saves") or 0)
            track_post_outcome(post_id=pid, reactions=counts.get("reactions", 0),
                               comments=counts.get("comments", 0), reposts=counts.get("reposts") or 0,
                               impressions=counts.get("impressions") or None,
                               saves=counts.get("saves") or 0, user_id=user_id)
            scraped += 1
        return f"Scraped stats for {scraped} post(s)"
    finally:
        quit_gracefully(driver)


_GROUP_ID_RE = re.compile(r"/groups/(\d+)")


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
def auto_post_to_group(self, user_id: int, group_id: str):
    """Publish one short, value-add (non-promotional) post into a group via its share box.
    Best-effort — the group composer selectors are validated in the live pass."""
    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Group Post")
    except Exception as e:
        log_error("Error getting profile for group post", exc=e, user_id=user_id, task_name="auto_post_to_group")
        return f"Failed: {e}"
    try:
        driver.get(f"https://www.linkedin.com/groups/{group_id}/")
        time.sleep(random.uniform(4, 7))
        text = _strip_non_bmp(generate_group_post(
            my_profile, prefs=get_engagement_preferences(user_id),
            profile_synthesis=get_or_create_profile_synthesis(user_id, my_profile)) or "")
        if not text.strip():
            return "No group post generated"
        # Open the group share box, type, and post (best-effort SDUI selectors).
        if click_first(driver, wait, [(By.XPATH,
                "//button[contains(normalize-space(),'Start a post') or contains(normalize-space(),'Start a public post') "
                "or contains(@aria-label,'Start a post') or contains(@aria-label,'Create a post') "
                "or (contains(normalize-space(),'Start a') and contains(normalize-space(),'post'))]")],
                       "Group share box", required=False) is None:
            return "Group share box not found"
        time.sleep(random.uniform(2, 3))
        box = find_first(driver, wait, [(By.CSS_SELECTOR, "div[role='textbox']")], "Group post editor",
                         visible_only=True, required=False)
        if box is None:
            return "Group post editor not found"
        box.click()
        box.send_keys(text)
        time.sleep(random.uniform(1, 2))
        if click_first(driver, wait, [(By.XPATH, "//button[normalize-space()='Post']")], "Group Post button",
                       required=False) is None:
            return "Group Post button not found"
        time.sleep(random.uniform(3, 5))
        return "Posted to group"
    except Exception as e:
        log_error("Group post error", exc=e, user_id=user_id, task_name="auto_post_to_group")
        return f"Error: {e}"
    finally:
        quit_gracefully(driver)


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_engage')
def automate_commenting(self, user_id: int, loop_for_duration: int = None, future_forward: int = 60):
    global stop_all_thread

    myprint("Starting Automate Commenting Thread...")

    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Auto Commenting")
    except Exception as e:
        log_error("Error while getting profile for auto commenting", exc=e, user_id=user_id, task_name="automate_commenting")
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
_GOLDEN_HOUR_MINUTES = 60
_GOLDEN_HOUR_REPLY_SWEEPS = 3
# Hard cap on scheduled sweeps — a misconfigured GOLDEN_HOUR_REPLY_SWEEPS can't flood the broker
# with ETA tasks or fragment the golden hour into meaninglessly tight intervals.
_GOLDEN_HOUR_MAX_SWEEPS = 12


def _golden_hour_sweep_countdowns(sweeps: int = _GOLDEN_HOUR_REPLY_SWEEPS,
                                  window_minutes: int = _GOLDEN_HOUR_MINUTES) -> list[int]:
    """Countdown seconds (from publish) for the golden-hour reply sweeps, spread evenly across the
    window so a comment left at any point in the golden hour is answered within window/sweeps minutes.
    e.g. 3 sweeps over 60 min → [1200, 2400, 3600] (20, 40, 60 min in). Sweep count is floored to 1
    and capped at _GOLDEN_HOUR_MAX_SWEEPS so misconfiguration can't schedule an unbounded burst."""
    n = max(1, min(_GOLDEN_HOUR_MAX_SWEEPS, int(sweeps)))
    step = (window_minutes * 60) / n
    return [int(round(step * i)) for i in range(1, n + 1)]


def _reply_to_comments_on_open_post(driver, wait, user_id: int, post_id: int, my_profile,
                                    profile_synthesis: str) -> str:
    """Navigate to the user's own post and reply to comments on it (thread-builder replies, plus
    reciprocity/lead-magnet handling). Shared by the per-post reply task and the recent-posts sweep.
    Returns a short human result string. Assumes the caller already has a live driver/profile."""
    post_url = get_post_url_from_log_for_user(user_id, post_id)
    if not post_url:
        myprint(f"No successful post URL for post {post_id}; skipping replies.")
        return "No post URL"
    # Ground replies in the canonical post body (posts table); fall back to the log message.
    post_message = get_post_content(post_id) or get_post_message_from_log_for_user(user_id, post_id)

    myprint(f"Replying to Comments of Post ID:{post_id} ...")
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
    path = urlparse(str(my_profile.profile_url)).path
    unique_url_name = path.split("/")[2] if len(path.split("/")) > 2 else None
    # LOOP SAFETY: without our slug we can't tell our own comments / already-replied ones apart, so a
    # sweep could reply to our own comments and re-reply every run. Fail SAFE — skip replying entirely.
    if not unique_url_name:
        log_warning("Reply sweep: could not resolve own profile slug — skipping replies to avoid "
                    "duplicate/self replies", user_id=user_id, post_id=post_id, action_type="reply")
        return "Skipped — no profile slug for dedup"

    comments_replied_count = 0
    lead_magnet = get_lead_magnet_settings(user_id)
    lead_magnet_blog_url = get_user_blog_url(user_id) if lead_magnet.get("enabled") else ""
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
        try:
            _link = comment.find_element(By.CSS_SELECTOR, "a[href*='/in/']")
            _ename = ((_link.text or "") or (_link.get_attribute("aria-label") or "")).strip().split("\n")[0]
            _eprofile = (_link.get_attribute("href") or "").split("?")[0]
            if _ename and _ename.lower() != (my_profile.full_name or "").lower():
                upsert_engager(user_id, _ename, _eprofile)
                if (lead_magnet.get("enabled") and lead_magnet.get("keyword") and lead_magnet.get("message")
                        and lead_magnet["keyword"].lower() in comment_text.lower()
                        and _eprofile and not has_received_lead_magnet(user_id, _eprofile)):
                    lm_message = render_dm_placeholders(
                        lead_magnet["message"],
                        first_name=(_ename or "").split(" ")[0],
                        blog_url=lead_magnet_blog_url or "")
                    send_private_dm.apply_async(kwargs={"user_id": user_id, "profile_url": _eprofile,
                                                        "message": lm_message})
                    record_lead_magnet_sent(user_id, _eprofile, post_id)
                    myprint(f"Lead magnet DM queued to {_ename} (keyword '{lead_magnet['keyword']}')")
        except Exception:
            pass
        # Already replied if our own profile link already appears in this comment's replies.
        already_replied = False
        if unique_url_name:
            try:
                already_replied = bool(comment.find_elements(By.CSS_SELECTOR, f"a[href*='{unique_url_name}']"))
            except Exception:
                already_replied = False
        if already_replied:
            myprint(f"We already replied to this comment: {short_comment_text}...")
            continue
        myprint(f"Responding to this comment: {short_comment_text}...")
        # Thread-builder: reply in a way that ends with a follow-up question so the commenter
        # replies again — first-hour thread depth is the top 2026 reach signal.
        response = generate_thread_reply(post_message, comment_text, my_profile,
                                         prefs=get_engagement_preferences(user_id),
                                         profile_synthesis=profile_synthesis)
        myprint(f"AI Generated Response to Comment: {response}")
        if response and _reply_to_comment_inline(driver, wait, comment, response, user_id=user_id):
            insert_new_log(user_id=user_id, post_id=post_id, action_type=LogActionType.REPLY,
                           result=LogResultType.SUCCESS, post_url=post_url, message=response)
            comments_replied_count += 1
            time.sleep(random.uniform(5, 12))
        else:
            insert_new_log(user_id=user_id, post_id=post_id, action_type=LogActionType.REPLY,
                           result=LogResultType.FAILURE, post_url=post_url, message=response)
    return f"Replied to {comments_replied_count} comments"


@shared_task.task(bind=True, base=QueueOnce,
                  once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id', 'sweep_slot']},
                  queue='se_engage')
def sweep_reply_comments(self, user_id: int, sweep_slot: int = 0):
    """Reply to new comments across the user's RECENT posts in ONE Selenium session. Triggered by a
    forwarded comment-notification email (event mode) or the scheduled dispatcher — replacing the old
    24h-per-post polling loop that drove the 429 rate-limiting. 429-safe: a rate-limited session logs
    a clean skip and returns (a later trigger/sweep retries). sweep_slot is part of the QueueOnce key
    so the golden-hour amplifier can enqueue several distinct sweeps for one user (same user_id+slot
    still dedups); the single-shot scheduled/API triggers leave it at 0."""
    prefs = get_engagement_preferences(user_id)
    days = int(prefs.get("reply_max_post_age_days") or 2)
    post_ids = get_recent_posted_post_ids(user_id, days=days)
    if not post_ids:
        return "No recent posts to sweep"
    try:
        driver, wait, _user_email, my_profile = get_current_profile(user_id=user_id, session_name="Reply Sweep")
    except LinkedInRateLimited as e:
        log_warning("Reply sweep skipped — LinkedIn rate-limited", exc=e, user_id=user_id,
                    task_name="sweep_reply_comments")
        return "Skipped — rate limited"
    except Exception as e:
        log_error("Error starting reply sweep", exc=e, user_id=user_id, task_name="sweep_reply_comments")
        return f"Failed to start reply sweep: {e}"
    try:
        profile_synthesis = get_or_create_profile_synthesis(user_id, my_profile)
        swept = 0
        for post_id in post_ids:
            try:
                _reply_to_comments_on_open_post(driver, wait, user_id, post_id, my_profile, profile_synthesis)
                swept += 1
            except Exception as e:
                log_warning("Reply sweep: post failed", exc=e, user_id=user_id, post_id=post_id,
                            task_name="sweep_reply_comments")
        return f"Swept replies on {swept}/{len(post_ids)} recent posts"
    finally:
        quit_gracefully(driver)


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

    result = "Automate Reply Commenting Task Started"

    try:

        start_time = datetime.now()

        # Stable VOICE synthesis reused across every reply in this run (voice source, not the raw JSON).
        profile_synthesis = get_or_create_profile_synthesis(user_id, my_profile)

        result = _reply_to_comments_on_open_post(driver, wait, user_id, post_id, my_profile, profile_synthesis)

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


def accept_connection_request(user_id: int):
    """Accept connection requests for the given user."""

    user_email, user_password = get_user_password_pair_by_id(user_id)

    driver, wait = get_driver_wait_pair(session_name='Accept Connection Requests', user_id=user_id)

    login_to_linkedin(driver, wait, user_email, user_password)

    # Navigate to the invitations manager page
    driver.get("https://www.linkedin.com/mynetwork/invitation-manager/")

    try:

        # Get all the invitation use the href and text (invitee name) to send a DM
        invitations = get_elements_as_list_wait_stale(wait,
                                                      "(//div[contains(@class,'invitation-card__container')]//div[contains(@class,'details')]//a)[2]",
                                                      "Finding Invitation Names and Urls")

        # For each invitation store the url and name to a dict using url as the key
        invitation_data = {invitation.get_attribute('href'): getText(invitation) for invitation in invitations}

        # Find and click all the accept buttons
        accept_buttons = get_elements_as_list_wait_stale(wait, '//button[contains(@aria-label,"Accept")]',
                                                         "Finding Accept Buttons")

        for accept_button in accept_buttons:
            accept_button.click()
            time.sleep(2)  # Wait for 2 seconds

    except Exception as e:
        log_error("Error while accepting connection requests", exc=e, user_id=user_id, action_type="accept_connection")
        invitation_data = {}
    finally:
        quit_gracefully(driver)

    # Return the invitations list
    return invitation_data


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
                           blog_url: str = "") -> str:
    """Single source of truth for filling DM / lead-magnet {placeholders}: {first_name},
    {headline}, {blog_url}. Used by BOTH the DM-template path and the Comment->DM lead magnet
    so their substitution can never drift. Tolerates unknown/malformed tokens gracefully."""
    if not text:
        return text or ""
    ctx = _SafePlaceholders(first_name=first_name or "there",
                            headline=headline or "my professional field",
                            blog_url=blog_url or "")
    try:
        return text.format_map(ctx)
    except (IndexError, ValueError):
        # malformed/positional braces (e.g. a stray "{") — replace known tokens only
        out = text
        for k in ("first_name", "headline", "blog_url"):
            out = out.replace("{" + k + "}", str(ctx[k]))
        return out


def build_dm_from_template(user_id: int, event_type: str, first_name: str,
                           my_profile: LinkedInProfile, step: int = 0, blog_url: str = "") -> "str | None":
    """Render the user's DM template for an event (filling {first_name}/{headline}/{blog_url})
    and LLM-refine it to their voice (<=300 chars). Falls back to the code-default template;
    returns None only when no template exists for that (event, step)."""
    tmpl = get_dm_template(user_id, event_type, step)
    if not tmpl:
        return None
    headline = getattr(my_profile, "job_title", None) or "my professional field"
    rendered = render_dm_placeholders(tmpl["template_text"], first_name=first_name,
                                      headline=headline, blog_url=blog_url)
    try:
        refined = get_ai_message_refinement(rendered, character_limit=300)
        # Humanization pass (issue #416 — A5): de-slop the DM before it's sent. Fails open and keeps
        # the pre-humanize text if a rewrite would exceed the 300-char DM budget.
        return humanize_text((refined or rendered).strip(), content_type="dm", max_chars=300)
    except Exception as e:
        log_warning("DM refinement failed; sending rendered template", exc=e, action_type="dm", user_id=user_id)
        return rendered.strip()


def enqueue_next_followup(user_id: int, profile_url: str, first_name: str, event_type: str, current_step: int) -> None:
    """If a follow-up template exists for the next step, schedule it at now + its delay_hours.
    due_at is stored as naive UTC to match the rest of the system (see get_due_followups)."""
    try:
        nxt = get_dm_template(user_id, event_type, current_step + 1)
        if nxt:
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            due = now_utc + timedelta(hours=int(nxt.get("delay_hours", 24) or 24))
            enqueue_followup(user_id, profile_url, first_name, event_type, current_step + 1, due)
    except Exception as e:
        log_warning("Failed to enqueue next follow-up", exc=e, action_type="followup", user_id=user_id)


# JS: walk the message list backwards for the most recent group's sender name. LinkedIn tags
# each message *group* with .msg-s-message-group__name; the outer li.msg-s-message-list__event
# carries no inbound/outbound marker, so the sender name is the reliable signal (confirmed on a
# live thread 2026-07-04). Continuation bubbles have no name → we scan back to the last named one.
_LAST_SENDER_JS = (
    "const ev=[...document.querySelectorAll('li.msg-s-message-list__event, .msg-s-event-listitem')];"
    "for(let i=ev.length-1;i>=0;i--){const n=ev[i].querySelector('.msg-s-message-group__name');"
    "if(n&&n.innerText.trim())return n.innerText.trim();}return null;")


def check_dm_replied(driver, wait, profile_url: str, my_name: str = None) -> bool:
    """Best-effort: has this person replied since our last message? Opens their message thread
    and finds the sender of the most recent message group — if it isn't us, they replied.
    Defensive — returns False (proceed with the follow-up) when it can't tell, logging a miss.
    DOM structure confirmed live; the full profile->Message->detect flow (and self-name match)
    still needs E2E validation before this is trusted to suppress follow-ups (see tracking issue)."""
    try:
        driver.get(profile_url)
        time.sleep(random.uniform(2, 4))
        msg_btn = find_first(driver, wait,
                             [(By.CSS_SELECTOR, "button[aria-label^='Message']"),
                              (By.XPATH, "//button[normalize-space()='Message']")],
                             "Open message thread", required=False)
        if msg_btn is None:
            return False
        driver.execute_script("arguments[0].click();", msg_btn)
        time.sleep(random.uniform(3, 5))
        last_sender = driver.execute_script(_LAST_SENDER_JS)
        if not last_sender:
            log_warning("Reply-detection: no message sender found", action_type="followup")
            return False
        if my_name and my_name.strip().lower() in last_sender.strip().lower():
            return False  # we spoke last → no reply yet
        return True  # someone other than us spoke last → they replied
    except Exception as e:
        log_warning("Reply-detection failed (assuming no reply)", exc=e, action_type="followup")
        return False


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='se_outreach')
def process_user_followups(self, user_id: int, max_per_run: int = 20):
    """Send this user's due DM follow-ups: skip (and stop the sequence) anyone who has replied,
    otherwise render the next-step template in the user's voice, send it, mark it sent, and
    schedule the following step."""
    due = [f for f in get_due_followups(datetime.now(timezone.utc).replace(tzinfo=None))
           if f["user_id"] == user_id]
    if not due:
        return "No due follow-ups"
    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Follow-ups")
    except Exception as e:
        log_error("Error getting profile for follow-ups", exc=e, user_id=user_id, task_name="process_user_followups")
        return f"Failed to start follow-ups: {e}"
    sent = 0
    try:
        for f in due[:max_per_run]:
            if check_dm_replied(driver, wait, f["profile_url"], my_name=getattr(my_profile, "full_name", None)):
                stop_followups_for_profile(user_id, f["profile_url"])
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
    return f"Sent {sent} follow-up(s)"


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

        # After Accepting a Connection Request:
        invitations_accepted = accept_connection_request(user_id)
        for profile_url, name in invitations_accepted.items():
            first_name = name.split(" ")[0]
            message = build_dm_from_template(user_id, "connection_accepted", first_name, my_profile)
            if message:
                send_private_dm.apply_async(kwargs={"user_id": user_id, "profile_url": profile_url, "message": message})
                enqueue_next_followup(user_id, profile_url, first_name, "connection_accepted", 0)

        # After Receiving a Recommendation — thank the recommender
        recommendations_received = get_recent_recommendations(driver, wait)
        for profile_url, name in recommendations_received.items():
            first_name = name.split(" ")[0]
            message = build_dm_from_template(user_id, "recommendation_received", first_name, my_profile)
            if message:
                send_private_dm.apply_async(kwargs={"user_id": user_id, "profile_url": profile_url, "message": message})
                enqueue_next_followup(user_id, profile_url, first_name, "recommendation_received", 0)

        # After a Successful Collaboration — express gratitude and offer to connect further
        recent_collaborators = get_recent_collaborators(driver, wait)
        for profile_url, name in recent_collaborators.items():
            first_name = name.split(" ")[0]
            message = build_dm_from_template(user_id, "collaboration", first_name, my_profile)
            if message:
                send_private_dm.apply_async(kwargs={"user_id": user_id, "profile_url": profile_url, "message": message})
                enqueue_next_followup(user_id, profile_url, first_name, "collaboration", 0)

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

    # Simulate reading the post
    read_time = simulate_reading_time(content) / 2
    myprint(f"Simulated Reading... for {read_time} seconds")
    time.sleep(read_time)

    # Simulate thinking time
    thinking_time = simulate_thinking_time()
    myprint(f"Simulated Thinking... for {thinking_time} seconds")
    time.sleep(thinking_time)

    # Generate AI response — pass the user's engagement preferences so this path honors
    # tone/comment_length/style/emoji/hashtag settings exactly like the feed-commenting path
    # (it previously generated with NO prefs, silently ignoring the user's voice settings).
    try:
        prefs = get_engagement_preferences(user_id)
    except Exception as e:
        log_warning("Could not load engagement preferences for comment; generating with defaults",
                    exc=e, user_id=user_id, action_type="comment")
        prefs = None
    comment_text = generate_ai_response(content, my_profile, img_url, prefs=prefs,
                                        profile_synthesis=profile_synthesis)

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

    myprint(f"Starting Profile Viewer DMs")

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

        while True:  # Keep looping until we find a viewed on date out of range
            # Get Each Viewer within the last day (or time of dm run via database log)
            viewer_elements = get_elements_as_list_wait_stale(wait,
                                                              '//ul[@aria-label="List of Entities"]//a[contains(@href,"linkedin.com/in") and not(contains(@aria-label,"Update"))]',
                                                              "Finding Profile Viewers")

            # myprint(f"Viewers count: {len(viewer_elements)}")

            if len(viewer_elements) > 0:
                # myprint("Here 1")
                # Get the last viewer
                last_viewer = viewer_elements[-1]
                # myprint("Here 2")
                # Extract the viewer's name
                name_element = last_viewer.find_element(By.XPATH,
                                                        './/div[contains(@class,"artdeco-entity-lockup__title")]/span/span[1]')
                # myprint("Here 3")
                if name_element:
                    last_viewer_name = getText(name_element)
                    # myprint(f"Last Viewer Name: {last_viewer_name}")
                else:
                    last_viewer_name = random.choice(["John", "Jane"]) + " Doe"
                    myprint("Could not find name of last viewer")

                last_viewed_on_element = last_viewer.find_element(By.XPATH, viewed_on_xpath)
                if last_viewed_on_element:
                    last_viewed_on = getText(last_viewed_on_element).strip()
                    # myprint(f"Last Viewed on: {last_viewed_on}")

                    # Convert viewed on to date
                    last_viewed_date = convert_viewed_on_to_date(last_viewed_on)
                    # myprint(f"Last Viewed on Date: {last_viewed_date}")

                    # if the last viewed on date is Greater than 24 hours break the while loop
                    if (datetime.now() - last_viewed_date).days > 1:
                        # myprint("Last viewed on date is more than 24 hours ago")
                        break  # Break the while loop
                else:
                    myprint(f"Could not find viewed on element for {last_viewer_name}")

                # Scroll down to get more elements
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(2)

            else:
                break  # Break the while loop

        # myprint(f"Viewers: {str(viewer_elements)}")
        result = f"Profile Viewer DMs Started. Found {len(viewer_elements)} viewers"
        myprint(f"Final Viewers count: {len(viewer_elements)}")

        try:
            # Filter the viewers by date within the last day
            viewer_elements = [e for e in viewer_elements if (datetime.now() - convert_viewed_on_to_date(
                getText(e.find_element(By.XPATH, viewed_on_xpath)))).days <= 1]
        except Exception as e:
            log_warning("Error filtering viewers by date", exc=e, user_id=user_id)

        myprint(f"Filtered Viewers count: {len(viewer_elements)}")

        current_tab = driver.current_window_handle
        handles = driver.window_handles

        # Get all the viewer names and urls into list so that elements don't go stale
        viewer_names = [
            getText(e.find_element(By.XPATH, './/div[contains(@class,"artdeco-entity-lockup__title")]/span/span[1]'))
            for e
            in viewer_elements]
        viewer_urls = [e.get_attribute('href') for e in viewer_elements]
        # Merge them into a dictionary to iterate over
        viewer_data = dict(zip(viewer_names, viewer_urls))

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
    myprint(f"Starting Profile Viewer Engagement")

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

                        first_name = viewer_name.split(" ")[0]
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
    """Cleans up stale invites that the user has sent. Not yet implemented — no-op stub."""

    pass


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
    if count_dms_sent_today(user_id) >= int(prefs.get("max_dms_per_day") or 0):
        myprint(f"send_scheduled_dm: daily DM cap reached for user {user_id}; deferring DM {dm_id}")
        update_scheduled_dm_status(dm_id, ScheduledDmStatus.APPROVED)  # retry on the next scan
        return f"Scheduled DM {dm_id} deferred (daily DM cap reached)"

    dm_sent = send_dm_now(user_id, dm["recipient_profile_url"], dm["message"])
    update_scheduled_dm_status(dm_id, ScheduledDmStatus.SENT if dm_sent else ScheduledDmStatus.FAILED)
    return f"Scheduled DM {dm_id} -> {'sent' if dm_sent else 'failed'}"


def invite_to_connect_now(user_id: int, profile_url: str, message: str = None) -> bool:
    """Core connect-invite send: open the profile, click Connect (+ optional note), log the result.
    Returns True on success. Shared by invite_to_connect (reactive profile-viewer flow) and
    send_connection_request (issue #398 approval-gated proactive flow) so both use the same send +
    log path (mirrors send_dm_now). Re-raises LinkedInRateLimited when the kill-switch / 429 breaker
    is open so callers can defer rather than record a false failure."""
    user_email, user_password = get_user_password_pair_by_id(user_id)

    driver, wait = get_driver_wait_pair(session_name='Invite to Connect', user_id=user_id)

    result = "Invitation to Connect Started"
    invite_sent = False

    try:

        login_to_linkedin(driver, wait, user_email, user_password)

        if profile_url != driver.current_url:
            # Open the profile URL
            driver.get(profile_url)

        myprint(f"Inviting to connect: {profile_url}")

        # Locate the connect button
        try:
            click_element_wait_retry(driver, wait, '//main//button[contains(@aria-label, "Invite ")]',
                                     "Finding Connect Button", max_retry=1, use_action_chain=True)

            myprint("Found Connect Button and clicked it")


        except Exception as ce:
            # If it doesn't exist click the more then find the connect button there
            try:
                # Click the last more button
                click_element_wait_retry(driver, wait,
                                         '//main//button[contains(@aria-label,"More actions")]',
                                         "Finding More Button", max_retry=1, use_action_chain=True)

                # driver.find_elements(By.XPATH, '//main//button[contains(@aria-label,"More actions")]')[-1].click()

                myprint("Found More Button and clicked it")

                # Click the last connect button
                click_element_wait_retry(driver, wait, '//main//div[contains(@aria-label,"connect")]',
                                         "Finding Connect Button", max_retry=1, use_action_chain=True)

                # driver.find_elements(By.XPATH, '//div[contains(@aria-label,"connect")]')[-1].click()

                myprint("Found Connect Button and clicked it")
            except Exception as e:
                log_error("Failed to find more or connect button", exc=e, user_id=user_id, action_type="invite_connect")
                result = f"Failed to find more or connect button: Error: {str(e)}"

        # If connection_message exist click the With note button
        if message:
            try:
                click_element_wait_retry(driver, wait, '//button[contains(@aria-label,"Add a note")]',
                                         "Finding Add a Note Button", use_action_chain=True)

                myprint("Found Add a Note Button and clicked it")

                # Find the message box
                message_box = click_element_wait_retry(driver, wait, '//textarea[@id="custom-message"]',
                                                       "Finding Message Box", use_action_chain=True)

                myprint("Found Message and clicked it")

                # Clear the message box
                message_box.clear()

                myprint("Cleared the message box")

                # Message must be less than 300 characters. Try 3 times to get a revised message under that limit
                for i in range(3):
                    # Check Message character length
                    if len(message) > 300:
                        message = get_ai_message_refinement(message, 300)
                    else:
                        break

                # Put the text in the message box
                message_box.send_keys(message)

                myprint("Added message to message box")

                # Sleep so send button can become active
                time.sleep(2)

                myprint("Waited for send button to activate")

                # Click the send button
                click_element_wait_retry(driver, wait,
                                         '//button[contains(@aria-label,"Send invitation")]',
                                         "Finding Send Connection Button", use_action_chain=True)

                myprint("Found Send Connection Button and clicked it")
                result = CONNECTION_REQUEST_SENT_MESSAGE
            except Exception as e:
                log_error("Failed to add a note to connection request", exc=e, user_id=user_id, action_type="invite_connect")
                result = f"Failed to Add a note. Error: {str(e)}"
        else:
            # Else click send connection
            try:
                click_element_wait_retry(driver, wait,
                                         '//button[contains(@aria-label,"Send without a note")]',
                                         "Finding Send Without Note Button", use_action_chain=True)

                myprint("Found Send Without a Note Button and clicked it")
                result = CONNECTION_REQUEST_SENT_MESSAGE
            except Exception as e:
                log_error("Failed to find send-without-note connection button", exc=e, user_id=user_id, action_type="invite_connect")
                result = f"Failed to find send without a note connection button. Error: {str(e)}"
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
    finally:
        quit_gracefully(driver)  # Close the driver

    return invite_sent


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'keys': ['user_id', 'profile_url']},
                  reject_on_worker_lost=True, rate_limit='1/m', queue='se_outreach')
def invite_to_connect(self, user_id: int, profile_url: str, message: str = None):
    """Send a LinkedIn connection request (reactive profile-viewer flow). Thin wrapper over
    invite_to_connect_now; a throttle / kill-switch defers silently."""
    try:
        sent = invite_to_connect_now(user_id, profile_url, message)
    except LinkedInRateLimited as e:
        myprint(f"invite_to_connect deferred (throttled): {e}")
        return "Invitation deferred (LinkedIn throttled)"
    return CONNECTION_REQUEST_SENT_MESSAGE if sent else "Connection Request Failed"


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
        sent = invite_to_connect_now(user_id, req["recipient_profile_url"], req["message"])
    except LinkedInRateLimited as e:
        myprint(f"send_connection_request: throttled, deferring {request_id}: {e}")
        update_connection_request_status(request_id, ConnectionRequestStatus.APPROVED)  # retry on next scan
        return f"Connection request {request_id} deferred (LinkedIn throttled)"
    update_connection_request_status(
        request_id, ConnectionRequestStatus.SENT if sent else ConnectionRequestStatus.FAILED)
    return f"Connection request {request_id} -> {'sent' if sent else 'failed'}"


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
        if count_comments_today(user_id) >= int(prefs.get("max_comments_per_day") or 0):
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
        if count_dms_sent_today(user_id) >= int(prefs.get("max_dms_per_day") or 0):
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


def get_current_profile(user_id: int, session_name: str = "Get Current Profile") -> Tuple[
    WebDriver, WebDriverWait, str, LinkedInProfile]:
    """Update the profile of the user"""

    myprint(f"Getting Updated Profile")

    user_email, user_password = get_user_password_pair_by_id(user_id)

    driver, wait = get_driver_wait_pair(session_name=session_name, user_id=user_id)

    # Login first — a failure here (e.g. HTTP 429 rate-limit, expired cookie) is fatal
    # for this run; abort cleanly so the caller backs off instead of hammering LinkedIn.
    try:
        login_to_linkedin(driver, wait, user_email, user_password)
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
            sweeps = int(_env_float("GOLDEN_HOUR_REPLY_SWEEPS", _GOLDEN_HOUR_REPLY_SWEEPS))
            # Distinct sweep_slot per sweep → distinct QueueOnce key, so celery-once enqueues all of
            # them; keyed only on user_id, the 2nd/3rd apply_async would be dropped as duplicates.
            for slot, countdown in enumerate(_golden_hour_sweep_countdowns(sweeps)):
                sweep_reply_comments.apply_async(kwargs={'user_id': user_id, 'sweep_slot': slot},
                                                 countdown=countdown)

        return f"Post successfully created"

    else:
        log_error("Failed to create post using /posts API endpoint", user_id=user_id, post_id=post_id, action_type="post", api_provider="linkedin")
        # Update DB with status=failed in the logs table
        insert_new_log(user_id=user_id, action_type=LogActionType.POST, result=LogResultType.FAILURE, post_id=post_id,
                       message="Failed to create post using /posts API endpoint.")

        return f"Failed to create post using /posts API endpoint"


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': False}, reject_on_worker_lost=True,
                  rate_limit='4/m', queue='se_outreach')
def automate_invites_to_company_page_for_user(self, user_id: int):
    """Send invites to the company page for the given user."""

    driver, wait = get_driver_wait_pair(session_name='Company Page Invites', user_id=user_id)

    try:

        invite_count = automate_invitations(driver, wait, user_id)

    except Exception as e:
        log_error("Error while inviting to company page", exc=e, user_id=user_id, task_name="automate_invites_to_company_page_for_user", action_type="company_invite")
        invite_count = 0
    finally:
        quit_gracefully(driver)

    result = f"Invited {invite_count if invite_count else 0} people to the company page."
    myprint(result)
    return result
