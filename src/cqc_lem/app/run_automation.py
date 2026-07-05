import hashlib
import inspect
import json
import math
import re
import random
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import List, Tuple
from urllib.parse import urlparse

from celery_once import QueueOnce
from cqc_lem.app.my_celery import app as shared_task
from cqc_lem.utilities.ai.ai_helper import generate_ai_response, get_ai_message_refinement, summarize_recent_activity, \
    ai_check_message_history, post_is_relevant, generate_newsletter_edition, generate_group_post, \
    generate_thread_reply, generate_seed_comment
from cqc_lem.utilities.date import convert_viewed_on_to_date
from cqc_lem.utilities.db import get_user_password_pair_by_id, get_user_id, insert_new_log, LogActionType, \
    get_engagement_preferences, count_comments_today, get_recent_engagers, upsert_engager, \
    get_newsletter_settings, mark_newsletter_published, \
    upsert_user_group, get_enabled_group_ids, record_post_stats, get_recent_posted_post_ids, \
    get_lead_magnet_settings, has_received_lead_magnet, record_lead_magnet_sent, \
    LogResultType, has_user_commented_on_post_url, get_post_url_from_log_for_user, get_post_message_from_log_for_user, \
    has_engaged_url_with_x_days, get_post_content, get_post_video_url, update_db_post_status, PostStatus, PostType, \
    get_dm_history_for_profile, get_post_status, get_user_blog_url, get_post_type, get_carousel_slides, \
    get_dm_template, enqueue_followup, get_due_followups, mark_followup, stop_followups_for_profile
from cqc_lem.utilities.linkedin.company_page_inviter import automate_invitations
from cqc_lem.utilities.linkedin.helper import login_to_linkedin, get_my_profile, get_linkedin_profile_from_url, \
    load_profile_for_user
from cqc_lem.utilities.linkedin.poster import share_on_linkedin, share_carousel_on_linkedin
from cqc_lem.utilities.linkedin.profile import LinkedInProfile
from cqc_lem.utilities.logger import myprint, log_error, log_info, log_warning
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
                  reject_on_worker_lost=True, rate_limit='4/m')
def comment_on_post(self, user_id: int, post_link: str, comment_text: str):
    """Post a comment to the given post link"""

    # Check the database logs to make sure user hasn't already commented on this post
    if has_user_commented_on_post_url(user_id, post_link):
        myprint("User has already commented on this post. Skipping...")
        return "User has already commented on this post. Skipping..."

    driver, wait = get_driver_wait_pair(session_name='Post Comment')

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

            # Update database with record of comment to this post
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


def _feed_post_key(author: str, content: str) -> str:
    """Stable-ish dedup key for a feed post (no permalink/urn exists in the SDUI DOM anymore)."""
    digest = hashlib.sha1(f"{author}|{content[:200]}".encode("utf-8", "ignore")).hexdigest()[:20]
    return f"feedpost://{digest}"


# Relative-age units → minutes. The SDUI card shows a token like "3h •", "5d •", "2w •", "10mo •".
_AGE_UNIT_MIN = {"s": 0, "m": 1, "h": 60, "d": 1440, "w": 10080, "mo": 43200, "y": 525600}
_AGE_TOKEN_RE = re.compile(r"^(\d+)\s?(mo|[smhdwy])", re.I)
_COMMENTS_RE = re.compile(r"([\d,]+)\s+comments?", re.I)
_REACTIONS_RE = re.compile(r"([\d,]+)\s+(?:reactions?|likes?)", re.I)
_IMPRESSIONS_RE = re.compile(r"([\d,]+)\s+impressions?", re.I)


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


def _post_social_counts(card) -> dict:
    """Best-effort reaction/comment/impression counts parsed from the card's social-counts bar text.
    Returns {reactions, comments, impressions} (0 on miss). Impressions show only on the author's own
    post detail page; reactions/comments feed the low-weight feed 'activity' scoring signal."""
    try:
        text = card.text or ""
    except Exception:
        return {"reactions": 0, "comments": 0, "impressions": 0}

    def _num(rx):
        m = rx.search(text)
        return int(m.group(1).replace(",", "")) if m else 0

    return {"reactions": _num(_REACTIONS_RE), "comments": _num(_COMMENTS_RE),
            "impressions": _num(_IMPRESSIONS_RE)}


# Feed-post prioritization weights (tunable). Recency dominates: golden-hour posts get 4–10× the
# algorithmic weight and the author is online to reply — which is the whole point (earn a thread).
_SCORE_W_RECENCY = 0.5
_SCORE_W_RELEVANCE = 0.2
_SCORE_W_RECIPROCITY = 0.2
_SCORE_W_ACTIVITY = 0.1
_RECENCY_HALFLIFE_MIN = 180.0  # exp decay: ~1.0 under an hour, ~0.37 at 3h, small by a day


def _recency_score(age_minutes) -> float:
    if age_minutes is None:
        return 0.4  # unknown age → mid-priority, never top
    return math.exp(-max(0, age_minutes) / _RECENCY_HALFLIFE_MIN)


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
    return (_SCORE_W_RECENCY * recency + _SCORE_W_RELEVANCE * relevance
            + _SCORE_W_RECIPROCITY * reciprocity + _SCORE_W_ACTIVITY * activity)


def _strip_non_bmp(text: str) -> str:
    # ChromeDriver's send_keys raises WebDriverException on non-BMP characters (most emoji), so
    # drop them — otherwise emoji-flavoured AI comments fail to type at all.
    return ''.join(c for c in (text or "") if ord(c) <= 0xFFFF)


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
    _switch_feed_to_recent(driver, wait)  # surface golden-hour posts; scoring still ranks them

    posted, seen, scrolls = 0, set(), 0
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
            key = _feed_post_key(author, content)
            if key in seen:
                continue
            if has_user_commented_on_post_url(user_id, key) or not _passes_hard_excludes(content, author, prefs):
                seen.add(key)
                continue
            age = _post_age_minutes(driver, card)
            counts = _post_social_counts(card)
            if age is not None and age > max_age_min:           # recency hard gate
                seen.add(key)
                continue
            if min_reactions and counts["reactions"] < min_reactions:
                seen.add(key)
                continue
            meta = {"author": author, "age_minutes": age, "comments": counts["comments"],
                    "reactions": counts["reactions"], "relevant": _literal_relevant(content, author, prefs)}
            candidates.append((_score_feed_post(meta, prefs, engagers), key, card, content, author, age))

        if candidates:
            candidates.sort(key=lambda c: c[0], reverse=True)
            score, key, card, content, author, age = candidates[0]
            seen.add(key)  # decided on this one either way
            # Full include check (may use the LLM topic classifier) only on the chosen post.
            if not post_matches_preferences(content, author, prefs):
                continue
            comment_text = generate_ai_response(content, my_profile, None, prefs=prefs)
            if comment_text:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", card)
                time.sleep(simulate_reading_time(content) / 2 + simulate_thinking_time())
                if post_comment_inline(driver, wait, card, comment_text, user_id=user_id):
                    insert_new_log(user_id=user_id, action_type=LogActionType.COMMENT,
                                   result=LogResultType.SUCCESS, post_url=key, message=comment_text)
                    posted += 1
                    myprint(f"Commented on {author or 'a'}'s post "
                            f"(score {score:.2f}, age {'?' if age is None else str(age) + 'm'}) ({posted}/{max_posts})")
                    time.sleep(random.uniform(6, 14))  # human pacing between comments
            continue  # DOM re-rendered / candidate consumed — re-gather from the top
        # nothing actionable in view — scroll to load more
        driver.execute_script("window.scrollBy(0, 1200);")
        scrolls += 1
        time.sleep(random.uniform(2.5, 4))
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
                  queue='selenium')
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


def _pin_own_comment(driver) -> bool:
    """Best-effort: pin our just-posted comment on our own post. The comment's overflow control
    (aria-label 'View more options for <name>'s comment.') is hover-hidden, so we JS-click it,
    then click 'Pin comment' in the menu. Non-fatal — the seed comment's value stands without it."""
    try:
        opened = driver.execute_script(
            "const b=[...document.querySelectorAll('button[aria-label]')].find(x=>{"
            "const a=(x.getAttribute('aria-label')||'').toLowerCase();"
            "return a.includes('options for') && a.includes('comment');});"
            "if(b){b.scrollIntoView({block:'center'}); b.click(); return true;} return false;")
        if not opened:
            return False
        time.sleep(random.uniform(1, 2))
        return bool(driver.execute_script(
            "const el=[...document.querySelectorAll(\"[role=menuitem],[role=menuitemradio],button,div,span,li,h5\")]"
            ".find(e=>/^pin\\b/i.test((e.innerText||'').trim()) && (e.innerText||'').trim().length<20);"
            "if(el){(el.closest('[role=menuitem]')||el).click(); return true;} return false;"))
    except Exception as e:
        log_warning("Pin own comment failed", exc=e, action_type="comment")
        return False


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id', 'post_id']},
                  queue='selenium')
def auto_seed_comment_on_post(self, user_id: int, post_id: int):
    """After the user's post publishes, leave a value-adding FIRST comment on it (an open question
    or a behind-the-scenes insight — no links) and pin it. Seeds the comment thread that drives
    reach, and beats LinkedIn's suppression of link-in-first-comment by adding real value instead."""
    post_url = get_post_url_from_log_for_user(user_id, post_id)
    post_message = get_post_message_from_log_for_user(user_id, post_id)
    if not post_url:
        return "No post URL yet for seed comment"
    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Seed Comment")
    except Exception as e:
        log_error("Error getting profile for seed comment", exc=e, user_id=user_id, task_name="auto_seed_comment_on_post")
        return f"Failed to start seed comment: {e}"
    try:
        driver.get(post_url)
        time.sleep(random.uniform(4, 7))
        seed = generate_seed_comment(post_message, my_profile, get_engagement_preferences(user_id))
        if not seed:
            return "No seed comment generated"
        card = find_first(driver, wait, [(By.CSS_SELECTOR, "div.feed-shared-update-v2"), (By.TAG_NAME, "main")],
                          "Post container", required=False) or driver.find_element(By.TAG_NAME, "body")
        if post_comment_inline(driver, wait, card, seed, user_id=user_id):
            insert_new_log(user_id=user_id, post_id=post_id, action_type=LogActionType.COMMENT,
                           result=LogResultType.SUCCESS, post_url=post_url, message=seed)
            time.sleep(random.uniform(2, 4))
            pinned = _pin_own_comment(driver)
            myprint(f"Seed comment posted on post {post_id} (pinned={pinned})")
            return f"Seed comment posted (pinned={pinned})"
        return "Seed comment failed to post"
    except Exception as e:
        log_error("Seed comment error", exc=e, user_id=user_id, post_id=post_id, task_name="auto_seed_comment_on_post")
        return f"Seed comment error: {e}"
    finally:
        quit_gracefully(driver)


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id']},
                  queue='selenium')
def auto_scrape_post_stats(self, user_id: int):
    """Capture reactions/comments for each of the user's recent posts (feeds personalized
    post-time recommendations). Reuses the social-count extraction on each post's detail page."""
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
            counts = _post_social_counts(container) if container is not None else {"reactions": 0, "comments": 0, "impressions": 0}
            record_post_stats(user_id, pid, counts["reactions"], counts["comments"],
                              impressions=counts.get("impressions") or None)
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
                  queue='selenium')
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
                  queue='selenium')
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
                  queue='selenium')
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
        text = _strip_non_bmp(generate_group_post(my_profile, prefs=get_engagement_preferences(user_id)) or "")
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
                  queue='selenium')
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


@shared_task.task(bind=True, base=QueueOnce,
                  once={'graceful': True, 'unlock_before_run': True, 'keys': ['user_id', 'post_id']},
                  queue='selenium')
def automate_reply_commenting(self, user_id: int, post_id: int, loop_for_duration: int = 60, future_forward=0):
    """Reply to recent comments left on the post recently posted"""

    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Reply to Comments")
    except Exception as e:
        log_error("Error while getting profile for reply commenting", exc=e, user_id=user_id, task_name="automate_reply_commenting")
        return f"Failed to start reply commenting: {e}"

    result = "Automate Reply Commenting Task Started"

    try:

        start_time = datetime.now()

        myprint(f"Replying to Comments of Post ID:{post_id} ...")

        # Use the user id and the post id to get the post_url from the database
        post_url = get_post_url_from_log_for_user(user_id, post_id)

        # Get the message content of the post
        post_message = get_post_message_from_log_for_user(user_id, post_id)

        if post_url:
            # Navigate to the Post
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
            result = f"Comments Found: {len(comments)}"

            # our profile slug — used to detect comments we've already replied to
            path = urlparse(str(my_profile.profile_url)).path
            unique_url_name = path.split("/")[2] if len(path.split("/")) > 2 else None

            comments_replied_count = 0
            lead_magnet = get_lead_magnet_settings(user_id)
            for comment in comments:
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
                            send_private_dm.apply_async(kwargs={"user_id": user_id, "profile_url": _eprofile,
                                                                "message": lead_magnet["message"]})
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
                                                 prefs=get_engagement_preferences(user_id))
                myprint(f"AI Generated Response to Comment: {response}")
                if response and _reply_to_comment_inline(driver, wait, comment, response, user_id=user_id):
                    insert_new_log(user_id=user_id, post_id=post_id, action_type=LogActionType.REPLY,
                                   result=LogResultType.SUCCESS, post_url=post_url, message=response)
                    comments_replied_count += 1
                    time.sleep(random.uniform(5, 12))
                else:
                    insert_new_log(user_id=user_id, post_id=post_id, action_type=LogActionType.REPLY,
                                   result=LogResultType.FAILURE, post_url=post_url, message=response)
                result = f"Replied to {comments_replied_count} comments"

        else:
            myprint("Could not find successful post for this user and post_id. Sleeping...")
            result = "Could not find successful post for this user and post_id. Sleeping..."

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

    driver, wait = get_driver_wait_pair(session_name='Accept Connection Requests')

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


def build_dm_from_template(user_id: int, event_type: str, first_name: str,
                           my_profile: LinkedInProfile, step: int = 0, blog_url: str = "") -> "str | None":
    """Render the user's DM template for an event (filling {first_name}/{headline}/{blog_url})
    and LLM-refine it to their voice (<=300 chars). Falls back to the code-default template;
    returns None only when no template exists for that (event, step)."""
    tmpl = get_dm_template(user_id, event_type, step)
    if not tmpl:
        return None
    headline = getattr(my_profile, "job_title", None) or "my professional field"
    ctx = {"first_name": first_name or "there", "headline": headline, "blog_url": blog_url or ""}
    try:
        rendered = tmpl["template_text"].format(**ctx)
    except (KeyError, IndexError, ValueError):
        # user template referenced an unknown {placeholder} — degrade to a minimal fill
        rendered = tmpl["template_text"].replace("{first_name}", ctx["first_name"])
    try:
        refined = get_ai_message_refinement(rendered, character_limit=300)
        return (refined or rendered).strip()
    except Exception as e:
        log_warning("DM refinement failed; sending rendered template", exc=e, action_type="dm", user_id=user_id)
        return rendered.strip()


def enqueue_next_followup(user_id: int, profile_url: str, first_name: str, event_type: str, current_step: int) -> None:
    """If a follow-up template exists for the next step, schedule it at now + its delay_hours."""
    try:
        nxt = get_dm_template(user_id, event_type, current_step + 1)
        if nxt:
            due = datetime.now() + timedelta(hours=int(nxt.get("delay_hours", 24) or 24))
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
                  queue='selenium')
def process_user_followups(self, user_id: int, max_per_run: int = 20):
    """Send this user's due DM follow-ups: skip (and stop the sequence) anyone who has replied,
    otherwise render the next-step template in the user's voice, send it, mark it sent, and
    schedule the following step."""
    due = [f for f in get_due_followups(datetime.now()) if f["user_id"] == user_id]
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
                  reject_on_worker_lost=True, rate_limit='2/m', queue='selenium')
def automate_appreciation_dms_for_user(self, user_id: int, loop_for_duration: int = None, future_forward: int = 60):
    user_email, user_password = get_user_password_pair_by_id(user_id)

    driver, wait = get_driver_wait_pair(session_name='Appreciation DMs')

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


def generate_and_post_comment(driver, wait, post_link, my_profile: LinkedInProfile) -> bool:
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

    # Generate AI response
    comment_text = generate_ai_response(content, my_profile, img_url)

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
                  queue='selenium')
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
                  reject_on_worker_lost=True, rate_limit='2/m', queue='selenium')
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

                    # Filter list to activities I haven't commented on
                    for activity in recent_activities:
                        link = str(activity.link)

                        # Leave comment on that activity
                        able_to_comment = generate_and_post_comment(driver, wait, link, my_profile)
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
                  rate_limit='2/m')
def clean_stale_invites(self, user_id: int):
    """Cleans up stale invites that the user has sent"""

    # TODO": Implement this method and
    # user_email, user_password = get_user_password_pair_by_id(user_id)

    # driver, wait = get_driver_wait_pair(session_name='Private DM')

    # login_to_linkedin(driver, wait, user_email, user_password)

    # quit_gracefully(driver)  # Close the driver

    pass


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True}, reject_on_worker_lost=True,
                  rate_limit='2/m', queue='selenium')
def send_private_dm(self, user_id: int, profile_url: str, message: str):
    """ Send dm message to a profile. Must be a 1st connection"""

    user_email, user_password = get_user_password_pair_by_id(user_id)

    driver, wait = get_driver_wait_pair(session_name='Private DM')

    login_to_linkedin(driver, wait, user_email, user_password)

    # Open the profile URL
    driver.get(profile_url)

    dm_sent = False

    myprint("Sending DM: " + message)

    final_result = "DM "

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

        final_result += " Sent Successfully"

    except Exception as e:
        final_result += f"Failed. Error: {str(e)}"

    finally:
        # Update DB logs with DM Sent
        insert_new_log(user_id=user_id, action_type=LogActionType.DM,
                       result=LogResultType.SUCCESS if dm_sent else LogResultType.FAILURE,
                       post_url=profile_url, message=message)

        quit_gracefully(driver)  # Close the driver

    myprint(f"{final_result}")
    return final_result


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True, 'keys': ['user_id', 'profile_url']},
                  reject_on_worker_lost=True, rate_limit='1/m', queue='selenium')
def invite_to_connect(self, user_id: int, profile_url: str, message: str = None):
    user_email, user_password = get_user_password_pair_by_id(user_id)

    driver, wait = get_driver_wait_pair(session_name='Invite to Connect')

    result = "Invitation to Connect Started"

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
                result = "Connection Request Sent Successfully"
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
                result = "Connection Request Sent Successfully"
            except Exception as e:
                log_error("Failed to find send-without-note connection button", exc=e, user_id=user_id, action_type="invite_connect")
                result = f"Failed to find send without a note connection button. Error: {str(e)}"
    except Exception as e:
        log_error("Error while inviting to connect", exc=e, user_id=user_id, action_type="invite_connect")
        result = f"Error while inviting to connect: {e}"
        insert_new_log(user_id=user_id, action_type=LogActionType.ENGAGED,
                       result=LogResultType.FAILURE, post_url=profile_url, message=str(e))
    else:
        insert_new_log(user_id=user_id, action_type=LogActionType.ENGAGED,
                       result=LogResultType.SUCCESS, post_url=profile_url, message=result)
    finally:
        quit_gracefully(driver)  # Close the driver

    return result


def final_method(drivers: List[WebDriver]):
    global stop_all_thread
    stop_all_thread.set()  # Set the flag to stop other threads
    for driver in drivers: quit_gracefully(driver)  # Quit all the drivers
    myprint("All drivers stopped. Program has exited.")
    sys.exit(0)


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': True}, reject_on_worker_lost=True,
                  rate_limit='1/m', queue='selenium')
def update_stale_profile(self, user_id: int):
    myprint(f"Updating Stale Profile. User ID: {user_id}")
    try:
        driver, wait, user_email, my_profile = get_current_profile(user_id=user_id, session_name="Update Stale Profile")
    except Exception as e:
        log_error("Error while updating stale profile", exc=e, user_id=user_id, task_name="update_stale_profile")
        return f"Failed to update profile: {e}"
    quit_gracefully(driver)
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
    myprint(f"Posting to LinkedIn: {content}")

    post_type = get_post_type(post_id)
    myprint(f"Post type: {post_type}")

    if post_type == PostType.CAROUSEL:
        slides = get_carousel_slides(post_id)
        myprint(f"Carousel slides ({len(slides)}): {slides}")
        # No slides, or no real per-slide images → don't post a placeholder carousel.
        # Flag the post 'error' so it surfaces for manual/dev fix instead of failing silently.
        urn = share_carousel_on_linkedin(user_id, content, slides) if slides else None
        if not urn:
            update_db_post_status(post_id, PostStatus.ERROR)
            log_error("Carousel has no real slide images — flagged 'error' for manual fix",
                      user_id=user_id, post_id=post_id, action_type="post", api_provider="linkedin")
            insert_new_log(user_id=user_id, action_type=LogActionType.POST, result=LogResultType.FAILURE,
                           post_id=post_id,
                           message="Carousel not posted: no real slide images. Status set to 'error'.")
            return f"Post {post_id} flagged 'error' — carousel had no usable images"
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

        # Purge local media now that LinkedIn has re-hosted it — keeps the assets
        # volume bounded. Best-effort: never let cleanup failure break posting.
        try:
            from cqc_lem.utilities.utils import purge_post_assets
            purge_post_assets(post_id, video_url=get_post_video_url(post_id) if post_type == PostType.VIDEO else None)
        except Exception as e:
            myprint(f"purge_post_assets failed for post_id={post_id}: {e}")

        # Update DB with status=success in the logs table and the post url
        insert_new_log(user_id=user_id, action_type=LogActionType.POST, result=LogResultType.SUCCESS, post_id=post_id,
                       post_url=post_url,
                       message=f"Successfully created post using /posts API endpoint.")

        # Schedule Reply to comments for 24 hours now that this has been posted
        base_kwargs = {
            'user_id': user_id,
            'post_id': post_id,
            'loop_for_duration': 60 * 60 * 24,
            'future_forward': 0
        }
        automate_reply_commenting.apply_async(kwargs=base_kwargs)

        return f"Post successfully created"

    else:
        log_error("Failed to create post using /posts API endpoint", user_id=user_id, post_id=post_id, action_type="post", api_provider="linkedin")
        # Update DB with status=failed in the logs table
        insert_new_log(user_id=user_id, action_type=LogActionType.POST, result=LogResultType.FAILURE, post_id=post_id,
                       message="Failed to create post using /posts API endpoint.")

        return f"Failed to create post using /posts API endpoint"


@shared_task.task(bind=True, base=QueueOnce, once={'graceful': False}, reject_on_worker_lost=True,
                  rate_limit='4/m', queue='selenium')
def automate_invites_to_company_page_for_user(self, user_id: int):
    """Send invites to the company page for the given user."""

    driver, wait = get_driver_wait_pair(session_name='Company Page Invites')

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
