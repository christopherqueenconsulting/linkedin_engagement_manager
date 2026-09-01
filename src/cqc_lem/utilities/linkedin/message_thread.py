"""The ONE way LEM opens (and reads) a 1:1 LinkedIn message thread — issue #731.

LinkedIn deliberately VARIES how the messaging entry point is exposed, and it rotates: by time, by
profile, and by connection state. The 2026-07-27 live probe found an ``<a href='/messaging/compose/…'>``
on a 1st-degree profile where `check_dm_replied` was still looking for a ``<button aria-label='Message'>``
— so the reply detector returned "no reply" for everybody and the follow-up sequencer kept messaging
people who had already answered. Hardcoding the anchor instead would just move that breakage to the
next rotation, so this module is a RESOLUTION LADDER: six independent routes, tried in order, each one
verified before it counts.

A route only succeeds when the thread is PROVABLY open: message events readable, or — on the chat
overlay alone — a compose form (`thread_reading`, judged by `is_open_thread`). A full-page compose
screen with zero message events is NOT a thread, and counting it as one let an earlier route suppress
`messaging_search`, the one route that can still find real history (issue #1851). Anything less
continues to the next route, and a ladder that exhausts every route is UNKNOWN, never "no reply": the
caller must treat unknown as *skip*, because a missed follow-up is recoverable and a follow-up sent to
someone who already replied is not.

The winning route is logged (`action_type='followup'`) so telemetry shows the rotation over time and
which fallbacks are actually carrying traffic — that is the early warning for the NEXT rotation.

Both messaging surfaces are handled: opening from a profile may yield the bottom-right chat OVERLAY
(``msg-overlay-*`` containers) rather than the full ``/messaging/`` page. The message events themselves
carry the same ``msg-s-*`` classes in both, so the reader is surface-agnostic and only the container
detection distinguishes them.
"""

import random
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Collection, Optional
from urllib.parse import quote, unquote

from bs4 import BeautifulSoup
from selenium.common import NoSuchElementException, StaleElementReferenceException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.wait import WebDriverWait

from cqc_lem.utilities.linkedin.helper import can_open_dm_thread
from cqc_lem.utilities.linkedin.scrapper import _degree_from_source
from cqc_lem.utilities.logger import log_debug, log_info, log_warning
from cqc_lem.utilities.selenium_util import find_first

MESSAGING_URL = "https://www.linkedin.com/messaging/"
COMPOSE_URL = "https://www.linkedin.com/messaging/compose/"

# Route ids — these are the telemetry vocabulary, so they are stable strings, not incidental labels.
ROUTE_ANCHOR = "anchor"
ROUTE_BUTTON = "button"
ROUTE_TEXT_NODE = "text_node"
ROUTE_OVERFLOW = "overflow"
ROUTE_DIRECT_URL = "direct_url"
ROUTE_MESSAGING_SEARCH = "messaging_search"

ROUTES = (ROUTE_ANCHOR, ROUTE_BUTTON, ROUTE_TEXT_NODE, ROUTE_OVERFLOW,
          ROUTE_DIRECT_URL, ROUTE_MESSAGING_SEARCH)

# How long one route's post-click render is given before it is declared a miss. The ladder walks up
# to six routes per person, so this is a POLL budget, not a WebDriverWait per locator: the DOM scan
# itself is instant and only the verification waits.
THREAD_RENDER_TIMEOUT_SECONDS = 8.0
_POLL_SECONDS = 0.5

SURFACE_OVERLAY = "overlay"
SURFACE_PAGE = "page"

# The visible label is nested in obfuscated-class span/div nodes under the control, so every locator
# here keys on href / aria-label / TEXT — never on a class name.
_ANCHOR_LOCATORS: list[tuple[str, str]] = [
    (By.CSS_SELECTOR, "main a[href*='/messaging/compose/']"),
    (By.CSS_SELECTOR, "a[href*='/messaging/compose/']"),
    (By.CSS_SELECTOR, "a[href*='/messaging/thread/']"),
    (By.XPATH, "//a[normalize-space()='Message']"),
]

_BUTTON_LOCATORS: list[tuple[str, str]] = [
    (By.CSS_SELECTOR, "button[aria-label^='Message']"),
    (By.XPATH, "//button[normalize-space()='Message']"),
]

# Tag-agnostic: the clickable ancestor of a node whose trimmed text is exactly 'Message'.
# ancestor-or-self is a REVERSE axis, so [1] is the NEAREST clickable ancestor, not the outermost.
_TEXT_NODE_LOCATORS: list[tuple[str, str]] = [
    (By.XPATH, "//*[normalize-space(text())='Message']"
               "/ancestor-or-self::*[self::a or self::button or @role='button'][1]"),
]

# The compose link LinkedIn's own top card renders carries TWO params, and both matter:
# `profileUrn` selects the thread, `recipient` is what actually ADDS the person to it. Built with
# `profileUrn` alone the page opens on an empty "Enter message recipients" field — a composer that is
# open but addressed to NOBODY (2026-08-04 grounding run). That reads as success to `thread_reading`,
# which is harmless for the read path but is the difference between sending and not sending.
_COMPOSE_SCREEN_CONTEXT = "NON_SELF_PROFILE_VIEW"

# Chrome that shares the recipient container with the pills. 'Enter message recipients' is the EMPTY
# state's placeholder — reading it as a name is exactly how an unaddressed composer would pass for an
# addressed one.
_RECIPIENT_CHROME_RE = re.compile(
    r"^(?:enter message recipients?|show suggested recipients.*|add a recipient.*|type a name.*)$",
    re.IGNORECASE)

_RECIPIENT_PILL_JS = (
    "const box=document.querySelector("
    "\"[class*='msg-connections-typeahead__added-recipients'], [class*='msg-compose__recipients']\");"
    "return box ? box.innerText : null;")

_MORE_LOCATORS: list[tuple[str, str]] = [
    (By.CSS_SELECTOR, "main button[aria-label^='More actions']"),
    (By.CSS_SELECTOR, "button[aria-label^='More actions']"),
    (By.XPATH, "//button[normalize-space()='More']"),
]

_SEARCH_LOCATORS: list[tuple[str, str]] = [
    (By.CSS_SELECTOR, "input[placeholder*='Search messages']"),
    (By.CSS_SELECTOR, "input[aria-label*='Search messages']"),
    (By.CSS_SELECTOR, "#search-conversations"),
    (By.CSS_SELECTOR, "input[type='search']"),
]

_CONVERSATION_LOCATORS: list[tuple[str, str]] = [
    (By.CSS_SELECTOR, "li.msg-conversation-listitem"),
    (By.CSS_SELECTOR, "[class*='msg-conversations-container'] li"),
    (By.CSS_SELECTOR, "li[class*='msg-conversation']"),
]

# Is a thread actually on screen? Message events carry the same msg-s-* classes on the full messaging
# page and inside the bottom-right overlay, so they are counted document-wide; only the CONTAINER
# tells the two surfaces apart. Whether a composer with zero events counts as an open THREAD is a
# per-surface question — see `is_open_thread`.
_THREAD_STATE_JS = (
    "const events=document.querySelectorAll("
    "'li.msg-s-message-list__event, .msg-s-event-listitem').length;"
    "const composer=!!document.querySelector("
    "\"div.msg-form__contenteditable, form.msg-form, [aria-label*='Write a message']\");"
    "const overlay=!!document.querySelector("
    "\"[class*='msg-overlay-conversation-bubble'], [class*='msg-overlay-container'], "
    "[class*='msg-overlay-list-bubble']\");"
    "return {events: events, composer: composer, overlay: overlay};")

# Walk the message list backwards for the most recent group's sender name. LinkedIn tags each message
# *group* with .msg-s-message-group__name; the outer li carries no inbound/outbound marker, so the
# sender name is the reliable signal. Continuation bubbles have no name → scan back to the last named.
_LAST_SENDER_JS = (
    "const ev=[...document.querySelectorAll('li.msg-s-message-list__event, .msg-s-event-listitem')];"
    "for(let i=ev.length-1;i>=0;i--){const n=ev[i].querySelector('.msg-s-message-group__name');"
    "if(n&&n.innerText.trim())return n.innerText.trim();}return null;")

# The BODY of the most recent message bubble in the open thread — the same DOM the reply detector
# reads. Inbound-intent detection (#483) and the #485 nurture draft both ride that one read.
_LAST_MESSAGE_JS = (
    "const ev=[...document.querySelectorAll('li.msg-s-message-list__event, .msg-s-event-listitem')];"
    "for(let i=ev.length-1;i>=0;i--){const b=ev[i].querySelector('.msg-s-event-listitem__body, "
    ".msg-s-event__content');if(b&&b.innerText.trim())return b.innerText.trim();}return null;")

# How far from the person's slug an embedded URN may sit and still be treated as theirs.
_URN_SLUG_WINDOW = 600

_PROFILE_URN_RE = re.compile(r"urn:li:fsd_profile:[A-Za-z0-9_-]+")
_SLUG_RE = re.compile(r"/in/([^/?#]+)")
_TRAILING_ID_RE = re.compile(r"-[0-9a-f]{4,}$", re.IGNORECASE)


class ThreadState(str, Enum):
    """What one reply check could actually establish. UNKNOWN is NOT 'no reply' — it means the thread
    could not be read, and the caller must skip rather than send blind.
    """
    REPLIED = "replied"
    NOT_REPLIED = "not_replied"
    UNKNOWN = "unknown"


@dataclass
class ThreadOpen:
    """The outcome of one ladder walk: whether a thread is verifiably open, and which route won."""
    opened: bool = False
    route: Optional[str] = None
    events: int = 0
    composer: bool = False
    surface: Optional[str] = None
    tried: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.opened


def resolve_self_name(user_id: Optional[int], my_profile=None) -> str:
    """The name to compare a thread's last sender against — '' when we genuinely don't know it.

    The SAVED display name wins over the scraped profile: it is the user's own declaration of what
    LinkedIn renders on their messages (Settings → Setup & Connection, required), while the scrape
    can be stale, cached from a failed refresh, or a placeholder. Falling back to the scrape keeps
    every account that never fills the field working exactly as it did.

    An empty result is NOT a name mismatch — `check_dm_replied` turns it into UNKNOWN, which skips
    the follow-up rather than sending one on a guess.
    """
    if user_id is not None:
        try:
            from cqc_lem.utilities.db import get_user_linkedin_display_name
            saved = (get_user_linkedin_display_name(user_id) or "").strip()
        except Exception as e:
            log_warning("Could not read the saved LinkedIn display name", exc=e, user_id=user_id,
                        action_type="followup")
            saved = ""
        if saved:
            return saved
    return (getattr(my_profile, "full_name", "") or "").strip()


def profile_slug(profile_url: str) -> str:
    """The /in/<slug> identity from a profile URL (lowercased, empty when there isn't one)."""
    m = _SLUG_RE.search(profile_url or "")
    return m.group(1).lower() if m else ""


def name_from_profile_url(profile_url: str) -> str:
    """A best-effort human name from a profile slug ('jane-doe-8a4b21' -> 'jane doe'), used only to
    seed the messaging SEARCH box when the caller has no stored name.
    """
    slug = profile_slug(profile_url)
    if not slug:
        return ""
    slug = _TRAILING_ID_RE.sub("", slug)
    return slug.replace("-", " ").strip()


def name_matches(needle: str, haystack: str) -> bool:
    """Whole-word containment, case- and whitespace-insensitive.

    Plain substring matching is wrong for names in BOTH places it is used: 'Jane' is a substring of
    'Janet Smith' (a stranger's conversation) and 'Chris' is a substring of 'Christine Baker' (their
    reply read as our own message, so the follow-up goes out anyway — the exact spam #731 exists to
    stop). A name must appear as its own word sequence to count.
    """
    needle = " ".join((needle or "").split()).lower()
    hay = " ".join((haystack or "").split()).lower()
    if not needle or not hay:
        return False
    return re.search(rf"(?<![^\W\d_]){re.escape(needle)}(?![^\W\d_])", hay) is not None


def _urn_near_slug(source: str, slug: str) -> Optional[str]:
    """The profile URN sitting NEAREST this person's slug in the embedded page model.

    LinkedIn's model puts a person's ``publicIdentifier`` beside their ``entityUrn``, so proximity to
    the slug is what identifies the URN as theirs.
    """
    best: Optional[tuple[int, str]] = None
    for hit in re.finditer(re.escape(slug), source, re.IGNORECASE):
        start = max(0, hit.start() - _URN_SLUG_WINDOW)
        window = source[start:hit.end() + _URN_SLUG_WINDOW]
        anchor = hit.start() - start
        for m in _PROFILE_URN_RE.finditer(window):
            distance = 0 if m.start() <= anchor <= m.end() else min(abs(m.start() - anchor),
                                                                    abs(m.end() - anchor))
            if best is None or distance < best[0]:
                best = (distance, m.group(0))
    return best[1] if best else None


def profile_urn_from_page(driver: WebDriver, profile_url: str = "") -> Optional[str]:
    """The person's ``urn:li:fsd_profile:*`` URN, for the direct-URL route.

    Prefer the compose anchor's own ``profileUrn`` query value — that is LinkedIn's own answer for
    THIS person. The page-model fallback is deliberately SLUG-SCOPED rather than "first URN in the
    document": a profile page also carries the viewer's own URN (the Me menu) and every 'People also
    viewed' card, so the first URN is routinely somebody else — and composing to it would open a
    stranger's thread whose last sender then decides THIS person's follow-up. Returns None rather
    than guessing, which drops the ladder to the messaging-search route.
    """
    try:
        for el in driver.find_elements(By.CSS_SELECTOR, "a[href*='profileUrn=']"):
            href = unquote(str(el.get_attribute("href") or ""))
            m = _PROFILE_URN_RE.search(href)
            if m:
                return m.group(0)
    except (WebDriverException, StaleElementReferenceException, NoSuchElementException) as e:
        log_debug("Could not extract profile URN from page links", exc=e, profile_url=profile_url)
    slug = profile_slug(profile_url)
    if not slug:
        return None
    try:
        return _urn_near_slug(unquote(driver.page_source or ""), slug)
    except (WebDriverException, AttributeError):
        return None


def compose_url_for(urn: str) -> str:
    """LinkedIn's OWN compose URL for a person, rebuilt from their profile URN.

    The `recipient` half is the URN's trailing id and is not optional — see `_COMPOSE_SCREEN_CONTEXT`.
    """
    ident = (urn or "").split(":")[-1]
    return (f"{COMPOSE_URL}?profileUrn={quote(urn, safe='')}"
            f"&recipient={quote(ident, safe='')}"
            f"&screenContext={_COMPOSE_SCREEN_CONTEXT}")


def composer_recipient(driver: WebDriver) -> str:
    """The name the OPEN composer is addressed to — '' when it is addressed to nobody.

    This is the send path's proof of delivery target. An empty string is never "probably fine": it is
    the unaddressed compose screen, and typing into it sends to whoever a typeahead happens to
    resolve, or to nobody at all.
    """
    try:
        raw = driver.execute_script(_RECIPIENT_PILL_JS)
    except Exception as e:
        log_warning("Could not read the message composer's recipient", exc=e, action_type="dm")
        return ""
    for line in str(raw or "").splitlines():
        line = " ".join(line.split())
        if line and not _RECIPIENT_CHROME_RE.match(line):
            return line
    return ""


@dataclass
class ComposerOpen:
    """The outcome of opening a composer for SENDING: open is not enough, it must be addressed."""
    opened: bool = False
    recipient: str = ""
    urn: Optional[str] = None
    surface: Optional[str] = None
    reason: Optional[str] = None

    @property
    def addressed(self) -> bool:
        """The only reading a sender may act on: the composer is open AND names somebody.

        `__bool__` delegates here, so `if open_addressed_composer(...)` is already the send gate —
        an open-but-unaddressed composer is falsy, because typing into it sends to whoever a
        typeahead resolves, or to nobody (issue #1030).
        """
        return self.opened and bool(self.recipient)

    def __bool__(self) -> bool:
        return self.addressed


def open_addressed_composer(driver: WebDriver, wait: WebDriverWait, profile_url: str,
                            person_name: Optional[str] = None, user_id: Optional[int] = None,
                            timeout: float = THREAD_RENDER_TIMEOUT_SECONDS) -> ComposerOpen:
    """Open a composer that is PROVABLY addressed to this person — the one way LEM reaches a DM it is
    about to send (issue #1030).

    Deliberately NOT `open_message_thread`. That ladder answers "can I read this thread", and its
    routes click whichever matching control the DOM offers first; for a READ a wrong thread yields a
    wrong verdict, but for a SEND it puts our message in a stranger's inbox — the #1012 hazard class.
    So this navigates instead of clicking: the person's URN is resolved from their OWN profile page
    (slug-scoped, `profile_urn_from_page`), the compose URL is rebuilt from it, and the composer's
    recipient pill is then read back as the outcome. Both halves have to hold. No URN, or a composer
    that names nobody, returns not-addressed and the caller must NOT send — refusing to congratulate
    someone is recoverable, messaging the wrong person is not.
    """
    result = ComposerOpen()
    try:
        driver.get(profile_url)
    except WebDriverException as e:
        log_warning("Could not open the profile to reach its message composer", exc=e,
                    user_id=user_id, action_type="dm")
        result.reason = "profile_unreachable"
        return result
    time.sleep(random.uniform(2, 4))

    urn = profile_urn_from_page(driver, profile_url)
    if not urn:
        log_warning(f"No profile URN on {profile_url} — cannot address a message to them",
                    user_id=user_id, action_type="dm")
        result.reason = "no_urn"
        return result
    result.urn = urn

    try:
        driver.get(compose_url_for(urn))
    except WebDriverException as e:
        log_warning("Could not open the message composer", exc=e, user_id=user_id, action_type="dm")
        result.reason = "compose_unreachable"
        return result

    reading = _wait_thread_open(driver, timeout)
    # Deliberately NOT `is_open_thread`: the SEND path wants exactly the full-page compose screen
    # that the read ladder rejects (issue #1851). It is not trusted on being open — the recipient
    # pill below is what proves it is addressed, which is a stronger check than message history.
    result.opened = bool(reading["events"] or reading["composer"])
    result.surface = reading.get("surface")
    if not result.opened:
        # DEBUG, not a warning: navigating straight to a person's compose URL is a best-effort
        # probe, and a composer that never renders is the expected outcome for anyone we can't
        # message this way (not a 1st-degree connection, InMail-only, messaging restricted) — not
        # evidence the selectors in `_THREAD_STATE_JS` rotted. `send_dm_now` already treats
        # `composer_missing` as a graceful non-send (issue #1710: this recurred 3x/24h and filed a
        # code defect for working refusal-to-send behavior, the #917/#1071 pattern).
        log_debug(f"The message composer never rendered for {profile_url}", user_id=user_id,
                  action_type="dm")
        result.reason = "composer_missing"
        return result

    result.recipient = composer_recipient(driver)
    if not result.recipient:
        log_warning(f"The message composer for {profile_url} names no recipient", user_id=user_id,
                    action_type="dm")
        result.reason = "unaddressed"
        return result

    log_info(f"Message composer addressed to '{result.recipient}' ({result.surface})",
             user_id=user_id, action_type="dm")
    return result


def thread_reading(driver: WebDriver) -> dict:
    """Raw verification read: message-event count, compose-form presence, and which surface rendered."""
    try:
        raw = driver.execute_script(_THREAD_STATE_JS) or {}
    except Exception as e:  # a JS failure is a failed READ, never a failed run
        log_warning("Could not read message-thread state", exc=e, action_type="followup")
        return {"events": 0, "composer": False, "surface": None}
    try:
        events = int(raw.get("events") or 0)
    except (TypeError, ValueError):
        events = 0
    composer = bool(raw.get("composer"))
    surface = None
    if events or composer:
        surface = SURFACE_OVERLAY if raw.get("overlay") else SURFACE_PAGE
    return {"events": events, "composer": composer, "surface": surface}


def _wait_thread_open(driver: WebDriver, timeout: float = THREAD_RENDER_TIMEOUT_SECONDS) -> dict:
    """Poll `thread_reading` until the thread renders (or the budget runs out). Bounded on purpose:
    the ladder has five more routes to try and cannot spend a WebDriverWait on each one.

    Message EVENTS end the wait; a bare composer does not. LinkedIn paints the compose form before
    the message list, so returning on the first composer would report a perfectly readable thread as
    empty — and an empty thread is UNKNOWN, which parks that person's follow-up until the DOM
    changes. A composer-only reading is held and returned only once the budget is spent.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    composer_only = None
    while True:
        reading = thread_reading(driver)
        if reading["events"]:
            return reading
        if reading["composer"] and composer_only is None:
            composer_only = reading
        if time.monotonic() >= deadline:
            return composer_only or reading
        time.sleep(_POLL_SECONDS)


def is_open_thread(reading: Optional[dict]) -> bool:
    """Does this verification reading prove a THREAD is open, or only that a composer rendered?

    Message EVENTS are proof on any surface — there is a conversation on screen. A composer with
    ZERO events is proof only on the OVERLAY surface: the bottom-right bubble is anchored to the
    person whose control we just clicked, so an empty one is a real thread we simply have no
    history in.

    On the full ``page`` surface the same reading is the standalone ``/messaging/compose/`` screen,
    which renders identically whether it is a real empty thread or a blank new-message form
    addressed to nobody — the surface affords no way to tell them apart. #1853 established that for
    `_try_direct_url`; the rule belongs to the READING rather than to one route id, because the
    profile's own Message anchor is an ``<a href='/messaging/compose/…'>`` that lands on the very
    same screen (issue #1851 follow-up).

    Nothing is lost when it genuinely was an empty thread: it carries no reply to detect either way,
    so the caller's UNKNOWN-and-skip is the same outcome it already reached.
    """
    if not reading:
        return False
    if reading.get("events"):
        return True
    if not reading.get("composer"):
        return False
    return reading.get("surface") == SURFACE_OVERLAY


def _verified_reading(reading: Optional[dict]) -> Optional[dict]:
    """The reading a ladder route may report as success — None when only a composer rendered.

    The rejection is DEBUG: reaching a compose screen instead of a thread is an ordinary step in the
    ladder, not selector rot, and the ladder as a whole already reports exhaustion at DEBUG (#1752).
    """
    if is_open_thread(reading):
        return reading
    if reading and reading.get("composer"):
        log_debug("A compose screen with no messages is not an open thread — continuing the ladder",
                  action_type="followup")
    return None


# How many times `read_last_sender` re-polls an empty read before giving up. `check_dm_replied`
# calls it the instant `open_message_thread` reports events, but LinkedIn paints the message
# bubbles before the separate call that attaches each group's sender name lands — so the very
# first read can land in that gap even though the thread is genuinely open and readable.
_SENDER_READ_RETRIES = 4


def read_last_sender(driver: WebDriver) -> str:
    """Name on the most recent message group of the ALREADY-OPEN thread ('' when unreadable).

    Retried briefly (issue #1864): a read taken right as the thread opens can catch the name
    field before its async attach finishes, which reads identically to a rotated selector
    (events present, sender empty) — the caller warns either way, so a transient race on every
    reply check was filing a `RecurringWarning` for working behaviour. A read that is STILL empty
    after the budget is spent is unchanged: '' , and the caller's warning stands.
    """
    for attempt in range(_SENDER_READ_RETRIES):
        try:
            sender = (driver.execute_script(_LAST_SENDER_JS) or "").strip()
        except Exception as e:
            log_warning("Could not read the last DM sender", exc=e, action_type="followup")
            return ""
        if sender or attempt == _SENDER_READ_RETRIES - 1:
            return sender
        time.sleep(_POLL_SECONDS)
    return ""


def read_last_message(driver: WebDriver) -> str:
    """Text of the newest message in the ALREADY-OPEN thread ('' when unreadable)."""
    try:
        return (driver.execute_script(_LAST_MESSAGE_JS) or "").strip()
    except Exception as e:
        log_warning("Could not read the last DM body", exc=e, action_type="dm")
        return ""


def _visible_elements(root, locators: list[tuple[str, str]]) -> list[WebElement]:
    """Displayed matches from the FIRST locator that yields any — the ladder needs a fail-FAST scan,
    so this never waits (routes get their patience in the post-click verification instead).
    """
    for find_by, value in locators:
        try:
            found = root.find_elements(find_by, value)
        except (WebDriverException, StaleElementReferenceException, NoSuchElementException):
            continue
        visible = []
        for el in found or []:
            try:
                if el.is_displayed():
                    visible.append(el)
            except (StaleElementReferenceException, WebDriverException):
                continue
        if visible:
            return visible
    return []


def _click(driver: WebDriver, element: WebElement) -> bool:
    """JS click first: the top-card controls sit under sticky headers that intercept native clicks."""
    try:
        driver.execute_script("arguments[0].click();", element)
        return True
    except (WebDriverException, StaleElementReferenceException):
        try:
            element.click()
            return True
        except (WebDriverException, StaleElementReferenceException):
            return False


def _try_control(driver: WebDriver, locators: list[tuple[str, str]], root=None,
                 timeout: float = THREAD_RENDER_TIMEOUT_SECONDS) -> Optional[dict]:
    """Click the first matching control and return the verification reading if a thread opened."""
    for element in _visible_elements(root if root is not None else driver, locators):
        if not _click(driver, element):
            continue
        verified = _verified_reading(_wait_thread_open(driver, timeout))
        if verified:
            return verified
    return None


def _try_overflow(driver: WebDriver, timeout: float) -> Optional[dict]:
    """LinkedIn demotes Message into the top-card **More** menu for some profiles/states. Several
    'More' controls can share a page, so each is opened in turn and routes 1-3 re-run inside it.
    """
    for menu in _visible_elements(driver, _MORE_LOCATORS):
        if not _click(driver, menu):
            continue
        time.sleep(_POLL_SECONDS)
        for locators in (_ANCHOR_LOCATORS, _BUTTON_LOCATORS, _TEXT_NODE_LOCATORS):
            reading = _try_control(driver, locators, timeout=timeout)
            if reading:
                return reading
    return None


def _try_direct_url(driver: WebDriver, timeout: float, urn: Optional[str] = None,
                    profile_url: str = "") -> Optional[dict]:
    """The strongest fallback: navigate straight to the compose URL built from the person's URN, so
    nothing depends on a rendered control at all.

    The URN is captured from the PROFILE page before any route clicks, because an earlier route may
    have navigated somewhere that no longer carries it; reading it again here is only the fallback.

    A composer with **zero message events does not count as opened here** (issue #1851). This URL is
    LinkedIn's compose surface, not a thread view: with no prior history it renders a blank compose
    form addressed to nobody-yet — a composer, not a thread — and that render is indistinguishable
    from a genuinely empty real thread. Rather than guess, a zero-event reading is treated as this
    route not working, so the ladder falls through to `messaging_search`, which either finds a real
    (possibly empty) conversation or leaves the caller at UNKNOWN — both already-correct outcomes.
    Accepting composer-only here was the bug: it returned `opened=True` with no sender to read,
    reported UNKNOWN, AND stopped the ladder before the one route most likely to find real history.
    That verdict now lives in `is_open_thread`, shared by every route, because the profile's own
    Message anchor lands on this same compose screen and could reproduce it one route earlier.
    """
    urn = urn or profile_urn_from_page(driver, profile_url)
    if not urn:
        return None
    driver.get(compose_url_for(urn))
    return _verified_reading(_wait_thread_open(driver, timeout))


def _try_messaging_search(driver: WebDriver, wait: WebDriverWait, person_name: Optional[str],
                          profile_url: str, timeout: float) -> Optional[dict]:
    """Slowest route, and the only one that works when the profile page offers nothing: open
    /messaging/, search the person by name, and open the matching conversation.

    Identifying the RIGHT conversation is the whole risk here — an opened thread is trusted, so a
    stranger's thread would decide this person's follow-up. A row that links to a different
    ``/in/<slug>`` is therefore rejected outright rather than falling back to its label, and the
    label itself must match a whole name ('Jane' does not match 'Janet Smith').
    """
    name = (person_name or "").strip() or name_from_profile_url(profile_url)
    if not name:
        return None
    driver.get(MESSAGING_URL)
    # warn_on_miss=False: this is the LAST route in the ladder, reached only once every earlier
    # route has already failed. A missing search box here is the expected shape of "the account
    # cannot message this person at all" (or the messaging SPA didn't boot, issue #1774) — the same
    # reasoning `open_message_thread` already applies to the ladder as a whole (issue #1752). Without
    # this flag the miss recurred into `RecurringWarning: Selector miss: Messaging search box`
    # (issue #1783) for that same working refusal-to-follow-up-blind behavior.
    box = find_first(driver, wait, _SEARCH_LOCATORS, "Messaging search box",
                     required=False, max_try=1, visible_only=True, warn_on_miss=False)
    if box is None:
        return None
    try:
        box.clear()
        box.send_keys(name)
        box.send_keys(Keys.ENTER)
    except (WebDriverException, StaleElementReferenceException):
        return None
    time.sleep(_POLL_SECONDS * 4)

    slug = profile_slug(profile_url)
    # The slug-derived name is usually FULLER than the stored first name, so both are accepted.
    derived = name_from_profile_url(profile_url)
    for item in _visible_elements(driver, _CONVERSATION_LOCATORS)[:10]:
        try:
            text = item.text or ""
            hrefs = " ".join(str(a.get_attribute("href") or "")
                             for a in item.find_elements(By.TAG_NAME, "a")).lower()
        except (WebDriverException, StaleElementReferenceException):
            continue
        if slug and f"/in/{slug}" in hrefs:
            pass  # the row names this person outright
        elif "/in/" in hrefs:
            continue  # it names SOMEBODY ELSE — never guess past that
        elif not (name_matches(name, text) or name_matches(derived, text)):
            continue
        if not _click(driver, item):
            continue
        verified = _verified_reading(_wait_thread_open(driver, timeout))
        if verified:
            return verified
    return None


def _profile_side_routes_worth_trying(driver: WebDriver, profile_url: str = "") -> bool:
    """Are routes 1-4 (the profile's OWN Message controls) worth attempting on this profile?

    A live grounding pass (#1857) found the top card renders a Message control on a 1st-degree
    profile and NONE at all on a 2nd/3rd-degree one — routes 1-4 cannot succeed there, so walking
    them anyway is four guaranteed misses plus a full page render before route five even starts.
    Reads the SAME page `driver.get(profile_url)` just rendered, via `_degree_from_source` — the
    grounded top-card read `parse_profile_header` already uses, so this adds no extra navigation
    and no second selector chain to drift out of sync with it.

    Fails OPEN: `can_open_dm_thread` treats an unreadable badge as unknown, never as "not
    connectable", so a selector drift here costs back the four wasted attempts it was meant to
    save, and never a missed follow-up.
    """
    try:
        degree = _degree_from_source(BeautifulSoup(driver.page_source, "html.parser"))
    except Exception as e:
        log_debug("Could not read the profile's connection degree; trying every route", exc=e,
                  profile_url=profile_url, action_type="followup")
        return True
    return can_open_dm_thread(degree)


def open_message_thread(driver: WebDriver, wait: WebDriverWait, profile_url: str,
                        person_name: Optional[str] = None, user_id: Optional[int] = None,
                        timeout: float = THREAD_RENDER_TIMEOUT_SECONDS,
                        skip_routes: Optional[Collection[str]] = None) -> ThreadOpen:
    """Open this person's 1:1 message thread, walking every known route until one VERIFIABLY works.

    Routes, in order: profile anchor → legacy button → tag-agnostic 'Message' text node → the
    top-card More menu → the direct compose URL built from the profile URN → messaging search.
    Returns a `ThreadOpen`; `opened` is False only when every route failed, and the winning route is
    logged so the next rotation shows up in telemetry rather than in user complaints.

    Routes named in `skip_routes` are not walked and are recorded in `ThreadOpen.skipped`. The
    read-only live probe uses this to stop before `messaging_search`, whose search box takes a typed
    name and commits on Enter — a write the probe must not make. Every name must be a real route id
    (`ROUTES`); an unknown name is a caller typo that would silently skip nothing, so it raises
    `ValueError` rather than letting the walk reach the route it meant to stop.

    Routes 1-4 are ALSO auto-skipped, the same way, whenever `_profile_side_routes_worth_trying`
    reads the just-rendered profile page as confidently not 1st degree (issue #1857): those routes
    only ever reach a control the top card renders for a 1st-degree connection, so walking them on
    anyone else is four guaranteed misses and a full page render for nothing. An unreadable badge
    changes nothing here — every route is still tried, exactly as before this existed.

    Every route exhausted is the EXPECTED outcome for anyone this account cannot message this way
    (not a 1st-degree connection, InMail-only, messaging restricted) — not evidence the selectors
    rotted (same reasoning as `open_addressed_composer`'s `composer_missing`, issue #1710). The
    caller (`check_dm_replied`) already turns this into `ThreadState.UNKNOWN` and skips the
    follow-up, so it is logged at DEBUG, not a WARNING that would recur into a `RecurringWarning`
    for working refusal-to-follow-up-blind behavior (issue #1752).
    """
    result = ThreadOpen()
    try:
        driver.get(profile_url)
    except WebDriverException as e:
        log_warning("Could not open the profile to reach its message thread", exc=e,
                    user_id=user_id, action_type="followup")
        return result
    time.sleep(random.uniform(2, 4))
    urn = profile_urn_from_page(driver)  # captured here: a clicked route may navigate away from it

    attempts = (
        (ROUTE_ANCHOR, lambda: _try_control(driver, _ANCHOR_LOCATORS, timeout=timeout)),
        (ROUTE_BUTTON, lambda: _try_control(driver, _BUTTON_LOCATORS, timeout=timeout)),
        (ROUTE_TEXT_NODE, lambda: _try_control(driver, _TEXT_NODE_LOCATORS, timeout=timeout)),
        (ROUTE_OVERFLOW, lambda: _try_overflow(driver, timeout)),
        (ROUTE_DIRECT_URL, lambda: _try_direct_url(driver, timeout, urn, profile_url)),
        (ROUTE_MESSAGING_SEARCH, lambda: _try_messaging_search(driver, wait, person_name,
                                                               profile_url, timeout)),
    )
    skip = set(skip_routes or ())
    unknown = skip - set(ROUTES)
    if unknown:
        raise ValueError(f"skip_routes names unknown route(s): {sorted(unknown)}; valid ids are "
                         f"{list(ROUTES)}")
    if not _profile_side_routes_worth_trying(driver, profile_url):
        profile_side = {ROUTE_ANCHOR, ROUTE_BUTTON, ROUTE_TEXT_NODE, ROUTE_OVERFLOW} - skip
        if profile_side:
            log_debug(f"{profile_url} is not 1st degree — skipping the profile-side Message "
                      f"routes {sorted(profile_side)}", user_id=user_id, action_type="followup")
        skip |= profile_side
    for route, attempt in attempts:
        if route in skip:
            result.skipped.append(route)
            log_debug(f"Skipping message-thread route '{route}'", user_id=user_id,
                      action_type="followup")
            continue
        result.tried.append(route)
        try:
            reading = attempt()
        except Exception as e:
            # One broken route must never end the ladder — that is the failure this module exists
            # to stop. Record it and keep walking.
            log_warning(f"Message-thread route '{route}' raised", exc=e, user_id=user_id,
                        action_type="followup")
            continue
        if reading:
            result.opened = True
            result.route = route
            result.events = int(reading.get("events") or 0)
            result.composer = bool(reading.get("composer"))
            result.surface = reading.get("surface")
            log_info(f"Message thread opened via '{route}' ({result.surface}, "
                     f"{result.events} message event(s))", user_id=user_id, action_type="followup")
            return result

    log_debug(f"No route opened a message thread for {profile_url}", user_id=user_id,
              action_type="followup", selectors=list(result.tried))
    return result
