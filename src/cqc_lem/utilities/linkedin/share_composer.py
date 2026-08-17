"""The LinkedIn share box, and the native "Celebrate an occasion" composer behind it (#1074, #1088).

`post_to_linkedin` publishes through the REST `/posts` API, which has no entity for an occasion —
so #1074 shipped the occasion archetypes as `manual_publish` drafts the author pastes into
LinkedIn's own composer by hand. This module is Phase 2's mechanics: the only route to that
composer is a browser, and driving it is exactly the sequence a person performs — Start a post →
More → Celebrate an occasion → pick the occasion → type → Post.

Mechanics only. No Celery, no policy, no DB: `app.engagement.posting.auto_publish_occasion_post`
owns the flag, the row's status and the cadence bounds, the same way `feed.auto_post_to_group` owns
the policy over `composer.py`'s typing mechanics.

Three things here are load-bearing, and each is one of #1013's invariants:

* **Success is the OUTCOME being present, never a click having landed.** `occasion_post_landed`
  re-reads the FEED for the post's own opening line; the Post button having been clicked proves
  nothing, and this lane cannot fall back to a URN because there is no API call to return one.
  An unconfirmed publish is `UNCONFIRMED`, never `PUBLISHED` — the caller leaves the row claimed
  and hands it to a human rather than re-running into a duplicate occasion post.
* **Never click a control whose label names a different entity than the target** (#1012). The
  occasion TYPE menu is the sharp edge: "Certification" sits next to "Educational milestone" and
  publishing the wrong one is a public claim the author never made. `OCCASION_TYPE_LABELS` is an
  exact, per-archetype allow-list, and a type that does not resolve aborts the run — it never
  settles for the neighbouring option.
* **Zero items is not "nothing to do" until the page agrees.** Every step that resolves nothing
  reports a `zero_walk` verdict against an anchor this chain does not use, so a rotated composer
  reads as drift instead of a quiet skip.

The share-box trigger chain lives here rather than in `app.engagement.feed` because two surfaces now
open the same control (the group share box and this one) and the live probe grounds ONE map —
`feed.py` imports it back under the private name its own body already used.
"""

import random
import time
from typing import NamedTuple, Optional

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

from cqc_lem.utilities.linkedin.cards import (
    _FEED_POST_TEXT_SEL,
    _X_LOWER_ARIA,
    _X_LOWER_TEXT,
    _normalize_post_text,
)
from cqc_lem.utilities.linkedin.zero_walk import grade_zero_walk, page_native_count
from cqc_lem.utilities.linkedin_formatter import strip_non_bmp
from cqc_lem.utilities.logger import log_debug, log_info
from cqc_lem.utilities.selenium_util import click_first, find_first

FEED_URL = "https://www.linkedin.com/feed/"

# --- the share box ------------------------------------------------------------------------------
# Moved here from `app.engagement.feed` (#1088) so the group composer, the occasion composer and the
# live probe all ground ONE chain. LinkedIn SDUI renders the trigger as a non-button clickable (e.g.
# `div[role='button']`) whose text is "Start a post"; the old `//button` chain missed it even though
# the page plainly contained the label (#1107).
SHARE_BOX_LOCATORS = [
    (By.XPATH,
     "//*[self::button or @role='button']["
     f"contains({_X_LOWER_TEXT},'start a post') "
     f"or contains({_X_LOWER_TEXT},'start a public post') "
     f"or contains({_X_LOWER_ARIA},'start a post') "
     f"or contains({_X_LOWER_ARIA},'create a post') "
     f"or (contains({_X_LOWER_TEXT},'start a') and contains({_X_LOWER_TEXT},'post'))"
     "]"),
]
SHARE_BOX_TEXT_SIGNALS = ("start a post", "start a public post", "create a post")

# --- the occasion composer ----------------------------------------------------------------------
# The composer shows a few attach affordances inline and files the rest behind an overflow control,
# so "Celebrate an occasion" is tried DIRECTLY first: opening a menu we did not have to is the same
# mistake `_attach_group_media` documents about the media overlay — one more surface to get back out
# of. Both chains are scoped to the open dialog, because an unscoped 'More' on a LinkedIn page
# belongs to some feed card's overflow menu.
OCCASION_ENTRY_LOCATORS = [
    (By.XPATH, "//div[@role='dialog']//*[self::button or @role='button']["
               f"contains({_X_LOWER_ARIA},'celebrate an occasion') "
               f"or contains({_X_LOWER_TEXT},'celebrate an occasion')]"),
    (By.XPATH, "//div[@role='dialog']//*[self::button or @role='button']["
               f"contains({_X_LOWER_ARIA},'celebrate') or contains({_X_LOWER_TEXT},'celebrate')]"),
]
OCCASION_MORE_LOCATORS = [
    (By.XPATH, "//div[@role='dialog']//*[self::button or @role='button']["
               f"contains({_X_LOWER_ARIA},'more') or normalize-space()='More']"),
]

# The occasion TYPE, per `content_framework` occasion archetype. Exact labels only, and never a
# near neighbour: LinkedIn's menu carries "Certification" and "New position" alongside these two,
# and clicking one of those publishes a claim about the author's life that nobody made (#1012).
# `tests/unit/utilities/test_occasion_composer.py` fails the build when an archetype in
# `OCCASION_FORMAT_KEYS` has no row here, so a third archetype cannot ship without its label.
OCCASION_TYPE_LABELS: dict = {
    "project_launch": ("project launch",),
    "educational_milestone": ("educational milestone",),
}

OCCASION_EDITOR_LOCATORS = [(By.CSS_SELECTOR, "div[role='dialog'] div[role='textbox']"),
                            (By.CSS_SELECTOR, "div[role='textbox']")]
OCCASION_POST_BUTTON_LOCATORS = [
    (By.XPATH, "//div[@role='dialog']//button[normalize-space()='Post']"),
    (By.XPATH, "//button[normalize-space()='Post']"),
]

# The page-native anchors the zero-walk cross-checks read. Each is INDEPENDENT of the chain it
# grades — asking a rotated chain about itself answers zero to both questions (#1013).
DIALOG_CONTROL_SEL = "div[role='dialog'] button, div[role='dialog'] [role='button']"
DIALOG_OPTION_SEL = ("div[role='dialog'] button, div[role='dialog'] [role='button'], "
                     "div[role='dialog'] [role='radio'], div[role='dialog'] li")

# What the run did. Only PUBLISHED means an occasion post is live.
PUBLISHED = "published"
NO_SHARE_BOX = "no_share_box"
NO_OCCASION_ENTRY = "no_occasion_entry"
NO_OCCASION_TYPE = "no_occasion_type"
NO_EDITOR = "no_editor"
NO_POST_BUTTON = "no_post_button"
UNCONFIRMED = "unconfirmed"
DRIVER_ERROR = "driver_error"

# How long the feed gets to render the post we just published before the run gives up confirming it.
# Generous on purpose: the cost of waiting is a slow task, and the cost of giving up early is a live
# post nothing recorded — which the caller then has to hand to a human.
_LANDING_POLLS = 10
_LANDING_POLL_SECONDS = (2.0, 3.5)
# How much of the post's opening the sighting matches on. Long enough that another card cannot
# collide with it, short enough to survive the card's own "…see more" truncation.
_LANDING_PROBE_CHARS = 60


class OccasionPublishResult(NamedTuple):
    """One attempt at the native occasion composer.

    Attributes:
        state: One of the module's state constants. `PUBLISHED` is the only one that means live.
        reason: A short, stable phrase for the log/DB row — never interpolated with volatile text,
            so the recurrence escalation can group it (see `utilities/CLAUDE.md`).
        zero_walk: The `zero_walk` verdict for the step that stopped the run, or None when nothing
            was graded (a clean publish, or a step that failed for a reason the page cannot answer).
    """

    state: str
    reason: str
    zero_walk: Optional[str] = None


def occasion_type_locators(labels) -> list:
    """Locators for ONE occasion type, built from its exact allow-listed labels.

    Args:
        labels: The lowercase labels this archetype may click, from `OCCASION_TYPE_LABELS`.

    Returns:
        A dialog-scoped locator chain, most exact first. Empty when no label was given, which is
        what makes an unmapped archetype resolve nothing instead of clicking the first option it
        finds.
    """
    locators = []
    for label in labels or ():
        text = str(label).strip().lower()
        if not text:
            continue
        locators.append((
            By.XPATH,
            "//div[@role='dialog']//*[self::button or @role='button' or @role='radio' or self::li]["
            f"{_X_LOWER_ARIA}='{text}' or {_X_LOWER_TEXT}='{text}']"))
    for label in labels or ():
        text = str(label).strip().lower()
        if not text:
            continue
        locators.append((
            By.XPATH,
            "//div[@role='dialog']//*[self::button or @role='button' or @role='radio' or self::li]["
            f"contains({_X_LOWER_ARIA},'{text}') or contains({_X_LOWER_TEXT},'{text}')]"))
    return locators


def landing_probe(text: str) -> str:
    """The normalized opening slice a sighting matches on, or '' when the text carries too little.

    Args:
        text: The post body as it was typed.

    Returns:
        A lowercase, whitespace-collapsed prefix of the body. Empty means no sighting is possible,
        which the caller must read as UNCONFIRMED rather than as a failed publish.
    """
    probe = _normalize_post_text(text or "")[:_LANDING_PROBE_CHARS].strip()
    # A handful of characters would match half the feed; a body that short is not a real post.
    return probe if len(probe) >= 20 else ""


def occasion_post_landed(driver, text: str, polls: int = _LANDING_POLLS,
                         sleep=time.sleep) -> Optional[bool]:
    """Is the post we just composed actually on the feed?

    Three-valued on purpose, because the two ways of not seeing it are different facts: True is a
    sighting, False is a feed that rendered cards without ours in them, and None is a read that
    could not be taken at all (nothing rendered, or the driver raised). Only True may mark the row
    posted; None grounds nothing and must never be reported as a failed publish.

    Args:
        driver: The Selenium driver, on any page — this navigates to the feed itself.
        text: The post body that was typed, used to build the sighting probe.
        polls: How many times the feed is re-read before giving up.
        sleep: Injection seam for the tests.

    Returns:
        True / False / None as above.
    """
    probe = landing_probe(text)
    if not probe:
        return None
    rendered_any = False
    for _ in range(max(1, polls)):
        sleep(random.uniform(*_LANDING_POLL_SECONDS))
        try:
            driver.get(FEED_URL)
            cards = driver.find_elements(By.CSS_SELECTOR, _FEED_POST_TEXT_SEL)
        except WebDriverException:
            continue
        for card in cards:
            try:
                body = _normalize_post_text(card.text or "")
            except WebDriverException:
                continue
            rendered_any = True
            if probe in body:
                return True
    return False if rendered_any else None


def _graded_miss(driver, state: str, reason: str, selector: str, what: str,
                 **context) -> OccasionPublishResult:
    """Grade a step that resolved nothing against a page-native anchor and package the result.

    The verdict is what separates "this composer has rotated" from "the page never loaded", and it
    is the reason a blocked run records the drift funnel instead of returning quietly.
    """
    verdict = grade_zero_walk(page_native_count(driver, selector), what, **context)
    return OccasionPublishResult(state, reason, verdict)


def open_share_box(driver, wait, user_id: int = None, post_id: int = None):
    """Click the feed's "Start a post" control.

    A miss never warns here: whether it is drift or a feed that never rendered is not something the
    chain can answer, and the caller grades it against the page's own card count instead — warning
    at both call sites would file a defect for a signed-out run (`utilities/CLAUDE.md`).

    Args:
        driver: The Selenium driver. Navigation to the feed is the caller's job.
        wait: The session's `WebDriverWait`.
        user_id: For the structured log context.
        post_id: For the structured log context.

    Returns:
        The clicked element, or None when the chain resolved nothing.
    """
    return click_first(driver, wait, SHARE_BOX_LOCATORS, "Share box", required=False,
                       warn_on_miss=False, user_id=user_id, post_id=post_id)


def publish_occasion_natively(driver, wait, archetype: str, text: str, user_id: int = None,
                              post_id: int = None, sleep=time.sleep) -> OccasionPublishResult:
    """Drive LinkedIn's native occasion composer for one drafted occasion post.

    The sequence is the human one: open the share box, reach "Celebrate an occasion" (directly, or
    behind the composer's overflow control), pick the occasion type this archetype maps to, type the
    body, press Post — then confirm the post is on the feed before calling it published.

    Never raises: every Selenium fault becomes a `DRIVER_ERROR` result, because the caller has a
    claimed DB row to resolve either way.

    Args:
        driver: The Selenium driver, already signed in.
        wait: The session's `WebDriverWait`.
        archetype: The post's `posts.archetype` — the key into `OCCASION_TYPE_LABELS`.
        text: The approved post body. Non-BMP characters are stripped here, because ChromeDriver's
            `send_keys` throws on them.
        user_id: For the structured log context.
        post_id: For the structured log context.
        sleep: Injection seam for the tests.

    Returns:
        An `OccasionPublishResult`. Only `PUBLISHED` means the post is live.
    """
    context = {"user_id": user_id, "post_id": post_id, "task_name": "auto_publish_occasion_post"}
    body = strip_non_bmp(text or "").strip()
    labels = OCCASION_TYPE_LABELS.get(str(archetype or "").strip().lower())
    if not body or not labels:
        # Neither is a page fact, so neither is graded: the caller checked both before opening
        # Chrome and this is the belt-and-braces refusal.
        return OccasionPublishResult(NO_OCCASION_TYPE, "occasion archetype has no composer mapping")

    try:
        driver.get(FEED_URL)
        sleep(random.uniform(4, 7))

        if open_share_box(driver, wait, user_id=user_id, post_id=post_id) is None:
            # Graded against the feed's own cards, not against the share-box text: a page that
            # rendered posts proves the run is signed in and the trigger is what rotated, while a
            # page that rendered nothing grounds nothing at all.
            return _graded_miss(
                driver, NO_SHARE_BOX, "share box not found",
                _FEED_POST_TEXT_SEL, "Occasion composer share box", **context)
        sleep(random.uniform(2, 3))

        entry = click_first(driver, wait, OCCASION_ENTRY_LOCATORS, "Celebrate an occasion",
                            required=False, warn_on_miss=False, max_try=1,
                            user_id=user_id, post_id=post_id)
        if entry is None:
            # The affordance is filed behind the composer's overflow control on most variants, so
            # a first miss is EXPECTED and must not warn — it is the reason the overflow exists.
            log_debug("Occasion entry not on the composer's first row — opening the overflow",
                      **context)
            click_first(driver, wait, OCCASION_MORE_LOCATORS, "Composer overflow", required=False,
                        warn_on_miss=False, max_try=1, user_id=user_id, post_id=post_id)
            sleep(random.uniform(1, 2))
            entry = click_first(driver, wait, OCCASION_ENTRY_LOCATORS, "Celebrate an occasion",
                                required=False, warn_on_miss=False, max_try=1,
                                user_id=user_id, post_id=post_id)
        if entry is None:
            return _graded_miss(
                driver, NO_OCCASION_ENTRY, "celebrate an occasion control not found",
                DIALOG_CONTROL_SEL, "Occasion composer entry", **context)
        sleep(random.uniform(2, 3))

        if click_first(driver, wait, occasion_type_locators(labels), "Occasion type",
                       required=False, warn_on_miss=False, user_id=user_id,
                       post_id=post_id) is None:
            # Deliberately terminal. The neighbouring options in this menu are other occasions —
            # settling for one would publish a claim about the author nobody made (#1012).
            return _graded_miss(
                driver, NO_OCCASION_TYPE, "occasion type not found",
                DIALOG_OPTION_SEL, "Occasion type menu", **context)
        sleep(random.uniform(2, 3))

        box = find_first(driver, wait, OCCASION_EDITOR_LOCATORS, "Occasion post editor",
                         visible_only=True, required=False, warn_on_miss=False,
                         user_id=user_id, post_id=post_id)
        if box is None:
            return _graded_miss(
                driver, NO_EDITOR, "occasion post editor not found",
                DIALOG_CONTROL_SEL, "Occasion post editor", **context)
        box.click()
        box.send_keys(body)
        sleep(random.uniform(1, 2))

        if click_first(driver, wait, OCCASION_POST_BUTTON_LOCATORS, "Occasion Post button",
                       required=False, warn_on_miss=False, user_id=user_id,
                       post_id=post_id) is None:
            return _graded_miss(
                driver, NO_POST_BUTTON, "occasion post button not found",
                DIALOG_CONTROL_SEL, "Occasion Post button", **context)
        sleep(random.uniform(3, 5))

        landed = occasion_post_landed(driver, body, sleep=sleep)
        if landed is True:
            log_info("Occasion post confirmed on the feed", **context)
            return OccasionPublishResult(PUBLISHED, "published natively")
        # The click landed and the outcome did not answer. That is NOT a failed publish — the post
        # may well be live — so the caller keeps the row claimed and asks a human, rather than
        # re-running into a duplicate occasion announcement.
        return OccasionPublishResult(UNCONFIRMED, "post not confirmed on the feed",
                                     "empty" if landed is False else "unknown")
    except WebDriverException as e:
        return OccasionPublishResult(DRIVER_ERROR, f"browser error: {type(e).__name__}")
