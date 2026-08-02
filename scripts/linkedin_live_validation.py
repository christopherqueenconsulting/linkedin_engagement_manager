#!/usr/bin/env python3
"""Live LinkedIn validation probe — R1 spike (issue #404).

Answers, against a REAL logged-in session, the two questions
``docs/LIVE_VALIDATION_FORMAT_AND_STATS.md`` cannot settle from the desk:

  1. Does a published native document post render as a DOCUMENT card (and what DOM tokens
     identify it), or as a multi-image share? — grounds C1 (#390).
  2. Which of reactions/comments/reposts/impressions/saves are actually scrapeable, and from
     which page — the post detail view or ``/analytics/post-summary/``? — grounds B2 (#387).
  3. On a post we already commented on: does the comment sort control exist, is our comment present
     under the default 'Most relevant' view, and does flipping to 'Most recent' surface it? —
     grounds the demotion signal in D4 (#628) before it is trusted to hold commenting.
  4. On a profile we have DM'd: WHICH route of the #731 message-thread ladder actually opens the
     thread today (anchor / button / text node / More menu / direct compose URL / messaging search),
     on which surface, and what the reply reader sees once it is open — the early warning for the
     next entry-point rotation, which is what silently disabled reply detection in the first place.
  5. On the home feed: does the "Sort by -> Recent" control resolve, and does the flip stick? —
     grounds #817. #622 made feed scoring recency-dominant, so a missing control means the whole
     matrix ranks LinkedIn's algorithmic feed instead of a fresh one.

**Read-only.** It navigates and reads: it publishes nothing, comments on nothing, sends no
invites or DMs and changes no settings. ``--probe-composer`` additionally OPENS the post
composer to capture the "add a document" affordance's anchors and closes it with Escape without
attaching or posting anything; ``--comment-outcome-url`` and ``--feed-sort`` flip a sort control,
exactly as the production readers they are grounding do.

Run it from inside a Selenium worker so the login/cookie/proxy stack is the production one
(``scripts/`` is not baked into the image, so pipe the file in on stdin, the same way
``weekly_linkedin_version_check.sh`` runs the version probe)::

    sudo docker exec -i celery_worker_selenium python - \
        --user-id 1 --post-url 'https://www.linkedin.com/feed/update/urn:li:activity:123/' \
        < scripts/linkedin_live_validation.py

The report is JSON on stdout — paste it into the issue. The parsing/verdict logic is pure and
unit-tested; the browser steps take injected callables so they are mocked in tests.

``--feed-sort`` deliberately runs against an image that PREDATES the chain it grounds: the only
grounding pass that can stop an unvalidated selector chain from shipping happens before the merge
that ships it. It carries the chain itself when the running image has none, and says so in the
report (``chain_source``).
"""

import argparse
import json
import sys
import time
from typing import Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

SIGNALS = ("reactions", "comments", "reposts", "impressions", "saves")
ANALYTICS_URL = "https://www.linkedin.com/analytics/post-summary/{urn}/"
FEED_URL = "https://www.linkedin.com/feed/"

# Whole-line labels worth echoing back with their neighbours. The analytics page renders each
# label and its value in separate elements, so seeing the neighbouring lines is what tells us
# whether the layout is still value-first ("72 / Impressions") or label-first ("Reposts / 0")
# — i.e. whether _stacked_counts still matches reality.
_LABELS = frozenset({"reactions", "comments", "reposts", "shares", "saves", "impressions"})

# A media container's identity lives in its data-testid / class / aria-label. Documents and
# image shares are DIFFERENT feed objects, so the token that appears here is the answer to "did
# this publish as a native document?".
_DOC_TOKENS = ("document",)
_IMAGE_TOKENS = ("image", "carousel")
_MEDIA_TOKENS = _DOC_TOKENS + _IMAGE_TOKENS

# Collect the media-bearing nodes under <main> without asserting any selector we have not seen
# live: match on the tokens above across the attributes SDUI actually keys off.
_MEDIA_JS = """
const out = [];
const root = document.querySelector('main') || document.body;
for (const el of root.querySelectorAll('[data-testid],[class],[aria-label]')) {
  const attrs = {
    tag: el.tagName.toLowerCase(),
    testid: el.getAttribute('data-testid') || '',
    cls: el.getAttribute('class') || '',
    aria: el.getAttribute('aria-label') || '',
  };
  const blob = (attrs.testid + ' ' + attrs.cls + ' ' + attrs.aria).toLowerCase();
  if (arguments[0].some(t => blob.includes(t))) { out.push(attrs); }
  if (out.length >= 40) { break; }
}
return out;
"""


def label_lines(text: Optional[str]) -> list[str]:
    """Every recognized count label with its neighbouring lines, as 'prev | line | next'.

    This is the drift check: the parser pairs a label with an adjacent bare count, so the raw
    neighbourhood is exactly what a reviewer needs to see when a signal comes back 0.
    """
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    out = []
    for i, line in enumerate(lines):
        if line.lower().rstrip(":") in _LABELS:
            prev = lines[i - 1] if i else ""
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            out.append(f"{prev} | {line} | {nxt}")
    return out


def signal_sources(detail: dict, analytics: dict) -> dict:
    """Per signal, which page actually yielded a non-zero value.

    'none' is a real finding, not a failure: it means that signal is not scrapeable from either
    page for this post (or the post genuinely has none of it — the raw lines disambiguate).
    """
    out = {}
    for signal in SIGNALS:
        detail_value = int(detail.get(signal) or 0)
        analytics_value = int(analytics.get(signal) or 0)
        if detail_value and analytics_value:
            source = "both"
        elif detail_value:
            source = "detail"
        elif analytics_value:
            source = "analytics"
        else:
            source = "none"
        out[signal] = {"detail": detail_value, "analytics": analytics_value,
                       "source": source, "value": max(detail_value, analytics_value)}
    return out


def classify_media_anchor(anchor: dict) -> str:
    """'document' / 'image' / 'unknown' for one captured media node."""
    blob = " ".join(str(v) for v in anchor.values()).lower()
    if any(token in blob for token in _DOC_TOKENS):
        return "document"
    if any(token in blob for token in _IMAGE_TOKENS):
        return "image"
    return "unknown"


def media_verdict(anchors: Optional[list[dict]]) -> str:
    """Overall render kind. A document post also carries image nodes (the rendered page
    thumbnails), so a single document token decides it; only the absence of one makes it an
    image share."""
    kinds = {classify_media_anchor(a) for a in anchors or []}
    if "document" in kinds:
        return "document"
    if "image" in kinds:
        return "image"
    return "unknown"


def find_document_affordance(labels: Optional[list[Optional[str]]]) -> Optional[str]:
    """The composer control that starts a document upload, by its visible/aria label."""
    for label in labels or []:
        if "document" in (label or "").lower():
            return label
    return None


def _social_counts(container) -> dict:
    # Imported lazily: the probe must exercise the SAME parser the scraper ships (#387), but
    # importing the Celery module at load time would drag the whole task graph into the tests.
    from cqc_lem.app.run_automation import _post_social_counts
    return _post_social_counts(container)


def _activity_urn(driver, post_url: str) -> Optional[str]:
    from cqc_lem.utilities.linkedin.poster import object_urn_from_post_url
    current = getattr(driver, "current_url", None)
    return (object_urn_from_post_url(current if isinstance(current, str) else "")
            or object_urn_from_post_url(post_url or ""))


def _read_main(driver, counts_fn) -> tuple:
    try:
        container = driver.find_element(By.TAG_NAME, "main")
    except Exception:
        return "", {}
    try:
        text = container.text or ""
    except Exception:
        text = ""
    return text, counts_fn(container)


def probe_post_stats(driver, post_url: str, counts_fn=_social_counts, sleep=time.sleep) -> dict:
    """B2: read the counts off the post detail page, then off the author's analytics page."""
    driver.get(post_url)
    sleep(5)
    detail_text, detail_counts = _read_main(driver, counts_fn)

    urn = _activity_urn(driver, post_url)
    analytics_text, analytics_counts = "", {}
    if urn:
        driver.get(ANALYTICS_URL.format(urn=urn))
        sleep(5)
        analytics_text, analytics_counts = _read_main(driver, counts_fn)

    return {"post_url": post_url, "activity_urn": urn,
            "signals": signal_sources(detail_counts, analytics_counts),
            "detail_lines": label_lines(detail_text),
            "analytics_lines": label_lines(analytics_text)}


def probe_document_render(driver, post_url: str, sleep=time.sleep) -> dict:
    """C1: capture the media anchors a published post renders, so 'native document vs
    multi-image share' stops being an assumption."""
    driver.get(post_url)
    sleep(5)
    try:
        anchors = driver.execute_script(_MEDIA_JS, list(_MEDIA_TOKENS)) or []
    except Exception as e:
        return {"post_url": post_url, "verdict": "unknown", "error": str(e), "anchors": []}
    return {"post_url": post_url, "verdict": media_verdict(anchors),
            "anchors": [dict(a, kind=classify_media_anchor(a)) for a in anchors]}


def probe_composer(driver, sleep=time.sleep) -> dict:
    """Open the feed composer, capture its attach-control labels (the document-upload anchors
    LEM has never needed, because documents publish through the API), then close it."""
    from cqc_lem.utilities.selenium_util import click_first, find_first
    from selenium.webdriver import ActionChains
    from selenium.webdriver.support.ui import WebDriverWait

    wait = WebDriverWait(driver, 10)
    driver.get(FEED_URL)
    sleep(5)
    # Same locator chain auto_post_to_group uses for the share box — one composer, one map.
    opened = click_first(driver, wait, [(By.XPATH,
        "//button[contains(normalize-space(),'Start a post') or contains(@aria-label,'Start a post') "
        "or contains(@aria-label,'Create a post')]")], "Composer share box", required=False)
    if opened is None:
        return {"opened": False, "controls": [], "document_affordance": None}
    sleep(3)

    dialog = find_first(driver, wait, [(By.CSS_SELECTOR, "div[role='dialog']")], "Composer dialog",
                        visible_only=True, required=False)
    controls = []
    root = dialog if dialog is not None else driver
    try:
        for button in root.find_elements(By.TAG_NAME, "button"):
            label = (button.get_attribute("aria-label") or button.text or "").strip()
            if label:
                controls.append(label)
    except Exception as e:
        # Best-effort capture: the composer re-renders while we enumerate, so a stale element
        # mid-loop is expected. Report whatever we collected instead of losing the whole probe.
        controls.append(f"<enumeration stopped: {type(e).__name__}>")

    try:
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    except Exception:
        # Closing the composer is courtesy only — the driver is quit right after in main(), and
        # a failed Escape must not mask the anchors this probe exists to report.
        pass
    return {"opened": True, "controls": controls,
            "document_affordance": find_document_affordance(controls)}


def comment_outcome_verdict(reading: Optional[dict]) -> str:
    """What one comment-outcome read proves about the 'Most relevant' demotion signal.

    'visible' / 'demoted' are the two real answers. Everything else is 'ambiguous', which is what
    the sweep persists as NULL — the point of this probe is to find out how often that happens
    BEFORE the demotion rate is trusted to hold a user's commenting.
    """
    reading = dict(reading or {})
    if not reading.get("sort_control_found"):
        return "ambiguous: no sort control"
    if reading.get("found_most_relevant"):
        return "visible"
    if not reading.get("switched_to_recent"):
        return "ambiguous: could not switch sort"
    if reading.get("found_most_recent"):
        return "demoted"
    return "ambiguous: comment not found in either sort"


def probe_comment_outcome(driver, post_url: str, our_slug: str, comment_text: str = "",
                          sleep=time.sleep) -> dict:
    """D4 (#628): on a post the user has ALREADY commented on, report what the outcome reader sees
    under each comment sort — so the demotion signal is grounded live before anything relies on it.

    Read-only: it navigates, scrolls, expands and flips the sort control. It posts nothing.
    """
    from cqc_lem.app.run_automation import (_comment_items, _comment_like_count, _comment_sort_label,
                                            _find_our_comment, _load_comment_thread,
                                            _post_author_href, _switch_comment_sort,
                                            _thread_replies, _SORT_MOST_RECENT)
    from selenium.webdriver.support.ui import WebDriverWait

    wait = WebDriverWait(driver, 10)
    driver.get(post_url)
    sleep(5)
    _load_comment_thread(driver)

    items = _comment_items(driver)
    reading = {"post_url": post_url, "our_slug": our_slug,
               "rendered_comments": len(items),
               "authors": [a for (_tb, _c, a) in items][:20],
               "sort_label": _comment_sort_label(driver, wait)}
    reading["sort_control_found"] = bool(reading["sort_label"])
    ours = _find_our_comment(items, our_slug, comment_text)
    reading["found_most_relevant"] = ours is not None and reading["sort_label"] == "most relevant"

    if ours is None and reading["sort_label"] == "most relevant":
        reading["switched_to_recent"] = _switch_comment_sort(driver, wait, _SORT_MOST_RECENT)
        if reading["switched_to_recent"]:
            _load_comment_thread(driver)
            items = _comment_items(driver)
            ours = _find_our_comment(items, our_slug, comment_text)
        reading["found_most_recent"] = ours is not None
    else:
        reading["switched_to_recent"] = False
        reading["found_most_recent"] = None

    if ours is not None:
        replies = _thread_replies(driver, ours, items)
        reading["like_count"] = _comment_like_count(driver, ours)
        reading["reply_authors"] = [a for (_c, a) in replies][:20]
        reading["post_author_href"] = _post_author_href(driver)
    reading["verdict"] = comment_outcome_verdict(reading)
    return reading


# This probe is piped into a RUNNING Selenium worker, so the `cqc_lem` it imports is the code baked
# into the DEPLOYED image — not this branch. That is exactly backwards for the only grounding pass
# that can stop an unvalidated chain from shipping: a PRE-merge run cannot import the chain it is
# grounding, because that image predates it. So the chain is taken from the running image when it
# HAS one and carried here when it does not, and the report names which — a reading against the
# deployed chain and one against this branch's answer different questions. `TestFeedSortChainCopy`
# fails the build if the carried copy drifts from `run_automation`'s.
_X_AZ_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_X_AZ_LOWER = "abcdefghijklmnopqrstuvwxyz"


def _x_lower(expression: str) -> str:
    return f"translate({expression},'{_X_AZ_UPPER}','{_X_AZ_LOWER}')"


_X_LOWER_TEXT = _x_lower("normalize-space()")
_X_LOWER_ARIA = _x_lower("@aria-label")
_X_LOWER_TESTID = _x_lower("@data-testid")

SORT_RECENT = "recent"
SORT_TOP = "top"
SORT_MISSING = "missing"
SORT_UNKNOWN = "unknown"

FALLBACK_SORT_LOCATORS = [
    (By.XPATH, f"//button[contains({_X_LOWER_ARIA},'sort')]"),
    (By.XPATH, f"//*[self::button or @role='button'][contains({_X_LOWER_TESTID},'sort')]"),
    (By.XPATH, f"//button[contains({_X_LOWER_TEXT},'sort by')]"),
    (By.XPATH, f"//button[@aria-haspopup][{_X_LOWER_TEXT}='{SORT_TOP}' or "
               f"{_X_LOWER_TEXT}='{SORT_RECENT}']"),
    (By.XPATH, f"//*[@role='button'][contains({_X_LOWER_ARIA},'sort')]"),
]

FALLBACK_RECENT_OPTION_LOCATORS = [
    (By.XPATH, "//*[self::button or self::li or @role='menuitem' or @role='menuitemradio' "
               f"or @role='option'][{_X_LOWER_TEXT}='{SORT_RECENT}']"),
    (By.XPATH, "//*[self::button or @role='menuitem' or @role='menuitemradio' or @role='option']"
               f"[contains({_X_LOWER_TEXT},'{SORT_RECENT}')]"),
    (By.XPATH, f"//*[{_X_LOWER_TEXT}='{SORT_RECENT}']"),
]


def feed_sort_chains() -> tuple:
    """(trigger locators, 'Recent' option locators, where they came from)."""
    try:
        from cqc_lem.app.run_automation import (_FEED_RECENT_OPTION_LOCATORS,
                                                _FEED_SORT_LOCATORS)
    except ImportError:
        return list(FALLBACK_SORT_LOCATORS), list(FALLBACK_RECENT_OPTION_LOCATORS), "script"
    return list(_FEED_SORT_LOCATORS), list(_FEED_RECENT_OPTION_LOCATORS), "image"


def control_sort_state(control) -> str:
    """Which sort a found control reports, or '' when its label is unreadable. '' is load-bearing:
    'we could not tell' must never be recorded as 'recent' — that is the lie #817 exists to stop.
    A label naming BOTH sorts is unreadable too, exactly as `_feed_sort_state` treats it, or the
    probe would report a control healthy that production reads as unknown."""
    if control is None:
        return ""
    try:
        label = f"{control.get_attribute('aria-label') or ''} {control.text or ''}".lower()
    except Exception:
        return ""
    has_recent, has_top = SORT_RECENT in label, SORT_TOP in label
    if has_recent and not has_top:
        return SORT_RECENT
    if has_top and not has_recent:
        return SORT_TOP
    return ""


def feed_sort_verdict(reading: Optional[dict]) -> str:
    """What one feed-sort probe proves about #817.

    'recent' is the only healthy answer: it is the state in which `_score_feed_post`'s recency term
    is ranking a recency-ordered feed. Everything else names WHICH half broke — no control at all
    (the selectors have rotated) versus a control that would not flip (the menu has) — because those
    have different fixes and the log line `Selector miss: Feed sort control` cannot tell them apart.
    """
    reading = dict(reading or {})
    state = reading.get("sort_after")
    note = (" [chain carried by this script — the running image predates #817]"
            if reading.get("chain_source") == "script" else "")
    if state == SORT_RECENT:
        route = ("already on Recent" if reading.get("sort_before") == SORT_RECENT
                 else "flipped to Recent")
        return f"sort control OK — {route}{note}"
    if not reading.get("control_found"):
        return ("NO sort control resolved — every feed scan is ranking LinkedIn's algorithmic feed; "
                f"re-ground _FEED_SORT_LOCATORS from `visible_controls` below{note}")
    if state == SORT_TOP or reading.get("option_found") is False:
        return ("control resolved but the 'Recent' option did not — re-ground "
                f"_FEED_RECENT_OPTION_LOCATORS from `visible_controls` below{note}")
    return f"control resolved, sort state unreadable after the flip ({state}){note}"


def menu_item_labels(driver, limit: int = 40) -> list:
    """Every visible menu-role label on screen. LinkedIn's sort options are not always <button>s, so
    `visible_button_labels` alone can report an empty menu that is actually rendered as list items —
    which would send the next re-grounding pass after the wrong half of the control.

    Menu ROLES are enumerated before bare <li>s, and the two are separate queries on purpose: one
    comma-joined selector returns DOCUMENT order, and a feed page is full of <li>s (global nav, the
    left rail, every post's action row) that come before an overlay dropdown — so the cap would be
    spent on page furniture and the menu this probe exists to capture could be missing from its own
    capture."""
    labels = []
    for selector in ("[role='menuitem'],[role='menuitemradio'],[role='option']", "li"):
        if len(labels) >= limit:
            break
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception as e:
            labels.append(f"<enumeration stopped: {type(e).__name__}>")
            break
        for element in elements:
            try:
                if not element.is_displayed():
                    continue
                label = (element.get_attribute("aria-label") or element.text or "").strip()
            except Exception:
                continue
            label = " ".join(label.split())[:80]
            if label and label not in labels:
                labels.append(label)
            if len(labels) >= limit:
                break
    return labels


def _flip_feed_sort(driver, wait, control, option_locators, sort_locators, sleep) -> dict:
    """The steps `_switch_feed_to_recent` takes, run from the probe so a pre-merge pass can take
    them against an image that does not have that function yet.

    It reports `option_found` separately, which the production return value has to collapse: 'top'
    covers both "the menu never opened" and "it opened without a Recent row", and those are re-ground
    from different halves of the capture below.
    """
    from cqc_lem.utilities.selenium_util import find_first

    if control is None:
        return {"option_found": None, "sort_after": SORT_MISSING}
    if control_sort_state(control) == SORT_RECENT:
        return {"option_found": None, "sort_after": SORT_RECENT}
    driver.execute_script("arguments[0].click();", control)
    sleep(1.5)
    option = find_first(driver, wait, option_locators, "Recent sort option", required=False,
                        visible_only=True, max_try=1)
    if option is None:
        # Returning here leaves the dropdown OPEN, which is what `visible_controls` needs to capture.
        return {"option_found": False, "sort_after": SORT_TOP}
    driver.execute_script("arguments[0].click();", option)
    sleep(3)
    after = find_first(driver, wait, sort_locators, "Feed sort control", required=False,
                       visible_only=True, max_try=1)
    return {"option_found": True, "sort_after": control_sort_state(after) or SORT_UNKNOWN}


def probe_feed_sort(driver, sleep=time.sleep) -> dict:
    """#817: on the real home feed, report whether the sort control resolves, what it reads before
    and after the flip, and every visible control that could plausibly BE it.

    `visible_controls` is the point: a bare "not found" is not re-groundable, but the live labels are
    exactly what the next locator chain gets written from. They are captured AFTER the flip attempt
    on purpose — when the trigger resolved and the 'Recent' option did not, the dropdown is still
    open at that moment, so the capture holds the menu this probe exists to re-ground.
    """
    from cqc_lem.utilities.selenium_util import find_first
    from selenium.webdriver.support.ui import WebDriverWait

    sort_locators, option_locators, chain_source = feed_sort_chains()
    wait = WebDriverWait(driver, 10)
    driver.get(FEED_URL)
    sleep(5)
    control = find_first(driver, wait, sort_locators, "Feed sort control", required=False,
                         visible_only=True, max_try=1)
    reading = {"url": getattr(driver, "current_url", FEED_URL),
               "chain_source": chain_source,
               "control_found": control is not None,
               "control": element_evidence(control) if control is not None else None,
               "sort_before": control_sort_state(control),
               "locators": [f"{by}={val}" for by, val in sort_locators]}
    try:
        reading.update(_flip_feed_sort(driver, wait, control, option_locators, sort_locators, sleep))
    except Exception as e:
        # The production path logs a warning and reports 'unknown'; a probe that swallowed the cause
        # would send the re-grounding pass looking at selectors when the session was the problem.
        reading.update({"option_found": None, "sort_after": SORT_UNKNOWN,
                        "flip_error": f"{type(e).__name__}: {e}"})
    reading["visible_controls"] = visible_button_labels(driver) + menu_item_labels(driver)
    reading["verdict"] = feed_sort_verdict(reading)
    return reading


def message_thread_verdict(reading: Optional[dict]) -> str:
    """What one thread-open probe proves about the #731 resolution ladder.

    The route that WON is the finding: 'anchor' is today's presentation, and a walk that only lands
    on 'direct_url' or 'messaging_search' means the profile-page controls have rotated again — the
    early warning this probe exists to give, one rotation BEFORE reply detection goes quiet.
    """
    reading = dict(reading or {})
    if not reading.get("opened"):
        return "no route opened a thread"
    route = reading.get("route")
    if not reading.get("events"):
        return f"opened via {route}, but no message events are readable (reply state = unknown)"
    if not (reading.get("self_name") or "").strip():
        return (f"opened via {route}, but no LinkedIn display name is saved for this user "
                f"(reply state = unknown — set it under Settings > Setup & Connection)")
    return f"opened via {route}"


def element_evidence(element) -> dict:
    """The attributes that prove WHICH control a route resolved to — the provenance a selector row
    needs. Every read is best-effort: an element that goes stale mid-capture must not lose the run."""
    out = {}
    for key, attr in (("tag", None), ("aria_label", "aria-label"), ("placeholder", "placeholder"),
                      ("role", "role"), ("type", "type")):
        try:
            value = element.tag_name if attr is None else element.get_attribute(attr)
        except Exception:
            value = None
        if value:
            out[key] = str(value)[:120]
    try:
        text = (element.text or "").strip()
    except Exception:
        text = ""
    if text:
        out["text"] = text[:80]
    return out


def visible_button_labels(driver, limit: int = 40) -> list:
    """Every visible button's label on the current screen.

    This is the EVIDENCE half of the publish verdict: 'Publish is UNKNOWN' is only believable
    alongside the list of controls that ARE on the editor screen. If a future run shows a publish
    control here, the two-screen assumption has changed and the ladder should gate on it again."""
    labels = []
    try:
        for button in driver.find_elements(By.TAG_NAME, "button"):
            try:
                if not button.is_displayed():
                    continue
                label = (button.get_attribute("aria-label") or button.text or "").strip()
            except Exception:
                continue
            if label and label not in labels:
                labels.append(label)
            if len(labels) >= limit:
                break
    except Exception as e:
        labels.append(f"<enumeration stopped: {type(e).__name__}>")
    return labels


# Candidate routes to a feed post's card root / text node. LinkedIn commonly keeps SEVERAL of
# these alive at once and rotates which is canonical, so the probe counts them all and the fix
# builds an ordered chain from whatever currently resolves. Ordered most-stable-first by kind:
# data-testid, then semantic/role, then the legacy hashed-class anchors that CLAUDE.md records as
# largely gone (kept precisely so a run can prove whether they are).
_CARD_ROOT_CANDIDATES = [
    ("testid_expandable_text", "[data-testid='expandable-text-box']"),
    ("testid_any_text_box", "[data-testid*='text-box']"),
    ("testid_update", "[data-testid*='update']"),
    ("legacy_update_v2_text", ".feed-shared-update-v2 .update-components-text"),
    ("legacy_update_v2", ".feed-shared-update-v2"),
    ("update_components_text", ".update-components-text"),
    ("data_urn", "[data-urn]"),
    ("data_id", "[data-id]"),
    ("article", "article"),
    ("comment_button", "button[aria-label='Comment']"),
    ("reaction_state_button", "button[aria-label^='Reaction button state']"),
    ("composer_textbox", "div[role='textbox']"),
]


def reaction_anchor_kind(evidence: dict) -> str:
    """Which of the three anchors `react_to_post_inline` needs this control could serve as.

    Names the ROLE rather than dumping raw attributes, so a run's output is directly comparable to
    the locator chain it is meant to re-ground (issue #816)."""
    blob = " ".join(str(evidence.get(k, "")) for k in ("aria_label", "text", "testid")).lower()
    if "reaction button state" in blob or "no reaction" in blob:
        return "state"                     # the pre/post-click 'Reaction state' read
    if "open reactions" in blob or "reactions menu" in blob:
        return "opener"                    # the fly-out opener
    if blob.strip() in {"like", "react like"} or blob.startswith("react "):
        return "toggle"                    # the default-Like toggle
    if any(r in blob for r in ("celebrate", "support", "love", "insightful", "funny")):
        return "flyout_option"             # a reaction inside the open fly-out
    if "like" in blob:
        return "like_like"                 # mentions like, but not one of the shapes above
    return "other"


def probe_feed_reactions(driver, max_cards: int = 3, open_menu: bool = False,
                         sleep=time.sleep) -> dict:
    """READ-ONLY capture of the feed cards' reaction controls (issue #816).

    All three anchors `react_to_post_inline` keys on are single locators with no fallback chain,
    and LinkedIn's SDUI has drifted away from every one of them (63 selector misses in 48h). This
    reports what is ACTUALLY on a card so the chain can be rebuilt against evidence instead of
    against a docstring that says 'verified live' about a long-gone DOM.

    It never leaves a reaction: it enumerates controls, and with --reaction-open-menu it will hover
    and open the fly-out (which changes no persisted state) to capture the option labels. The
    reaction buttons themselves are never clicked.
    """
    from selenium.webdriver.common.action_chains import ActionChains

    out: dict = {"cards": [], "opened_flyout": False, "note": "read-only; no reaction was left"}
    try:
        driver.get("https://www.linkedin.com/feed/")
        sleep(5)
        # The feed lazy-loads: without a scroll the first paint can carry no post cards at all, and
        # "found nothing" would then be indistinguishable from "the anchors are gone".
        for _ in range(3):
            driver.execute_script("window.scrollBy(0, 900);")
            sleep(2)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    # Always record WHICH screen we landed on. A zero-card result is only actionable next to the
    # evidence of what was actually rendered — an auth wall and a drifted card selector look
    # identical in a bare `cards_found: 0`.
    try:
        out["url"] = str(driver.current_url or "")[:200]
        out["title"] = str(driver.title or "")[:120]
    except Exception:
        pass

    # Count EVERY candidate, not just the one production happens to use. LinkedIn rotates these
    # anchors and often keeps several routes to the same control alive at once, so the useful
    # output is a ranked menu of what currently resolves — that is what a fallback chain is built
    # from. Reporting only the production selector's count answers "is it broken?" and nothing
    # about what to replace it with.
    for name, sel in _CARD_ROOT_CANDIDATES:
        try:
            out.setdefault("candidate_counts", {})[name] = len(
                driver.find_elements(By.CSS_SELECTOR, sel))
        except Exception as e:
            out.setdefault("candidate_counts", {})[name] = f"<{type(e).__name__}>"

    # Then walk the production path itself: post text node -> nearest ancestor carrying a Comment
    # button. Uses the SHIPPED constant when the running image has it, so the probe can never drift
    # from the code it is grounding (and says which it used).
    try:
        from cqc_lem.app.run_automation import _FEED_POST_TEXT_SEL as prod_sel
        out["text_sel_source"] = "image"
    except Exception:
        prod_sel = ("[data-testid='expandable-text-box'], "
                    ".feed-shared-update-v2 .update-components-text")
        out["text_sel_source"] = "probe-carried"
    out["text_sel"] = prod_sel

    try:
        boxes = driver.find_elements(By.CSS_SELECTOR, prod_sel)
        out["textboxes_found"] = len(boxes)
        cards = []
        for box in boxes:
            if len(cards) >= max_cards:
                break
            try:
                card = driver.execute_script(
                    "let el=arguments[0],d=0;while(el&&d<15){"
                    "if(el.querySelector&&el.querySelector(\"button[aria-label='Comment']\"))return el;"
                    "el=el.parentElement;d++;}return null;", box)
            except Exception:
                card = None
            if card is not None:
                cards.append(card)
    except Exception as e:
        out["error"] = f"card enumeration failed: {type(e).__name__}: {e}"
        return out
    out["cards_found"] = len(cards)

    # The card walk climbs to the nearest ancestor carrying a comment affordance, so when it fails
    # the decisive evidence is what that affordance ACTUALLY looks like now. Capture every control
    # that mentions "comment" on any of its three identity attributes, so the replacement chain is
    # built from observed anchors rather than from another guess.
    controls = []
    try:
        for button in driver.find_elements(By.CSS_SELECTOR, "button, [role='button']"):
            if not _safe_displayed(button):
                continue
            ev = element_evidence(button)
            try:
                testid = button.get_attribute("data-testid")
                if testid:
                    ev["testid"] = str(testid)[:120]
            except Exception:
                pass
            blob = " ".join(str(v) for v in ev.values()).lower()
            if "comment" in blob:
                if ev not in controls:
                    controls.append(ev)
            if len(controls) >= 8:
                break
    except Exception as e:
        controls.append({"error": f"{type(e).__name__}: {e}"})
    out["comment_controls"] = controls

    if not cards:
        # Nothing to sample: hand back the screen's own controls so the next run knows whether this
        # was an auth wall, an empty feed, or a card selector that has itself drifted.
        out["visible_buttons"] = visible_button_labels(driver, limit=30)
        try:
            out["body_text_head"] = (driver.find_element(By.TAG_NAME, "body").text or "")[:400]
        except Exception:
            pass

    for index, card in enumerate(cards):
        entry: dict = {"index": index, "controls": []}
        try:
            for button in card.find_elements(By.CSS_SELECTOR, "button, [role='button']"):
                try:
                    if not button.is_displayed():
                        continue
                except Exception:
                    continue
                evidence = element_evidence(button)
                try:
                    testid = button.get_attribute("data-testid")
                    if testid:
                        evidence["testid"] = str(testid)[:120]
                except Exception:
                    pass
                blob = " ".join(str(v) for v in evidence.values()).lower()
                # Only reaction-ish controls: a whole card's buttons is mostly noise.
                if not any(t in blob for t in ("react", "like", "celebrate", "support", "love",
                                               "insightful", "funny")):
                    continue
                evidence["kind"] = reaction_anchor_kind(evidence)
                entry["controls"].append(evidence)
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
        entry["kinds"] = sorted({c.get("kind", "other") for c in entry["controls"]})
        out["cards"].append(entry)

    if open_menu:
        # Hovering reveals the opener; opening the fly-out reveals the per-reaction buttons. Neither
        # persists anything on LinkedIn — only a click on a reaction would, and we never do that.
        #
        # Falls back to a DOCUMENT-level search when card enumeration found nothing. That is the
        # case worth capturing: the reaction anchors can be perfectly healthy while the card walk
        # that scopes `parent_element=card` is what actually broke, and a probe that gives up with
        # the cards cannot tell those two apart.
        try:
            scope = cards[0] if cards else driver
            out["flyout_scope"] = "card" if cards else "document"
            trigger = None
            for candidate in scope.find_elements(By.CSS_SELECTOR, "button, [role='button']"):
                if not _safe_displayed(candidate):
                    continue
                ev = element_evidence(candidate)
                if reaction_anchor_kind(ev) in {"toggle", "opener", "state"}:
                    trigger = candidate
                    out["flyout_trigger"] = ev
                    break
            if trigger is not None:
                ActionChains(driver).move_to_element(trigger).perform()
                sleep(2)
                out["flyout_candidates"] = [
                    element_evidence(b) for b in driver.find_elements(By.CSS_SELECTOR, "button")
                    if _safe_displayed(b) and reaction_anchor_kind(element_evidence(b)) in
                    {"flyout_option", "opener"}
                ][:15]
                out["opened_flyout"] = True
        except Exception as e:
            out["flyout_error"] = f"{type(e).__name__}: {e}"

    # The verdict the fix needs: which of the three anchors exist ANYWHERE on the sampled cards.
    seen = {k for card in out["cards"] for k in card.get("kinds", [])}
    out["anchors_present"] = {
        "state": "state" in seen,
        "opener": "opener" in seen,
        "toggle": "toggle" in seen,
    }
    return out


def _safe_displayed(element) -> bool:
    try:
        return bool(element.is_displayed())
    except Exception:
        return False


def article_editor_reading(verdict: dict) -> str:
    """One sentence a human can act on, so the JSON does not have to be interpreted."""
    if not verdict.get("editor_ready"):
        return (f"editor screen NOT usable — {verdict.get('first_missing')} resolved by no route; "
                "newsletter publishing cannot start until that step has a working route")
    publish = (verdict.get("publish") or {}).get("verdict")
    if publish == "OK":
        return "editor screen usable, and a publish control is present on it too"
    return ("editor screen usable (title, body and Next all resolved); publish is UNKNOWN by design "
            "— it lives in the dialog Next opens and this probe never clicks Next")


def probe_article_editor(driver, editor_url: str = "https://www.linkedin.com/article/new/",
                         sleep=time.sleep) -> dict:
    """#771/#804: open LinkedIn's article editor (read-only) and report which selector routes resolve
    for each publish step: title, body, next, publish. The editor is left untouched; nothing is typed,
    no control is clicked and nothing is published.

    Because nothing is clicked, only the EDITOR screen is ever rendered — so publish is graded
    UNKNOWN rather than MISSING (`on_editor_screen=True`). `buttons` records what WAS on the screen,
    which is what makes that grading checkable rather than assumed."""
    from cqc_lem.utilities.linkedin.article_editor import (find_article_editor_elements,
                                                           article_editor_verdict)
    from selenium.webdriver.support.ui import WebDriverWait

    wait = WebDriverWait(driver, 10)
    driver.get(editor_url)
    sleep(6)
    editor_map = find_article_editor_elements(driver, wait)
    verdict = article_editor_verdict(editor_map, on_editor_screen=True)
    for key, resolved in (("title", editor_map.title), ("body", editor_map.body),
                          ("next", editor_map.next_button), ("publish", editor_map.publish_button)):
        if resolved.element is not None:
            verdict[key]["element"] = element_evidence(resolved.element)
    verdict["url"] = getattr(driver, "current_url", editor_url)
    verdict["buttons"] = visible_button_labels(driver)
    verdict["verdict"] = article_editor_reading(verdict)
    return verdict


def probe_message_thread(driver, profile_url: str, person_name: str = "", self_name: str = "",
                         sleep=time.sleep) -> dict:
    """#731: walk the message-thread resolution ladder against a REAL profile and report which route
    resolved, which surface rendered (overlay vs full page), and what the reply reader then sees.

    `self_name` is the saved LinkedIn display name, so the probe also answers the OTHER half of a
    live reply check: does the name the user typed into Settings actually match what LinkedIn writes
    on their own messages? A mismatch reads as UNKNOWN in production and silently stops follow-ups.

    Read-only: it opens the thread and reads it. It types nothing into the composer and sends nothing.
    """
    from cqc_lem.utilities.linkedin.message_thread import (open_message_thread, read_last_sender,
                                                           profile_urn_from_page)
    from selenium.webdriver.support.ui import WebDriverWait

    wait = WebDriverWait(driver, 10)
    opened = open_message_thread(driver, wait, profile_url, person_name=person_name or None)
    reading = {"profile_url": profile_url,
               "opened": opened.opened,
               "route": opened.route,
               "routes_tried": list(opened.tried),
               "surface": opened.surface,
               "events": opened.events,
               "composer": opened.composer,
               "self_name": self_name or "",
               "profile_urn": profile_urn_from_page(driver, profile_url)}
    sleep(1)
    reading["last_sender"] = read_last_sender(driver) if opened.opened else ""
    reading["reply_state"] = _reply_state(reading)
    reading["verdict"] = message_thread_verdict(reading)
    return reading


def _reply_state(reading: dict) -> str:
    """The three-valued verdict `check_dm_replied` would return from this same reading — so the probe
    reports the DECISION, not just the DOM. 'unknown' here is a live warning that the sequencer is
    skipping this person (unreadable thread, or a saved display name that doesn't match).

    It runs the SAME whole-word comparison production uses, or the probe would report a verdict the
    sequencer never reaches."""
    from cqc_lem.utilities.linkedin.message_thread import name_matches

    last_sender = (reading.get("last_sender") or "").strip()
    self_name = (reading.get("self_name") or "").strip()
    if not reading.get("opened") or not last_sender or not self_name:
        return "unknown"
    return "not_replied" if name_matches(self_name, last_sender) else "replied"


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only live LinkedIn validation probe (#404)")
    parser.add_argument("--user-id", type=int, default=1, help="user whose session drives the probe")
    parser.add_argument("--post-url", help="permalink of one of that user's OWN published posts")
    parser.add_argument("--probe-composer", action="store_true",
                        help="also open the post composer to capture document-upload anchors")
    parser.add_argument("--comment-outcome-url",
                        help="permalink of someone else's post this user has commented on (#628)")
    parser.add_argument("--our-slug", help="the user's /in/<slug> (defaults to their profile URL)")
    parser.add_argument("--comment-text", default="",
                        help="the comment we left, so the reader can match the right one")
    parser.add_argument("--dm-thread-url",
                        help="profile URL of someone this user has DM'd — reports which message-thread "
                             "route resolves (#731)")
    parser.add_argument("--dm-thread-name", default="",
                        help="that person's name, for the messaging-search fallback route")
    parser.add_argument("--article-editor-url", default=None, const="https://www.linkedin.com/article/new/",
                        nargs="?",
                        help="open LinkedIn's article editor and report each publish step's selector "
                             "state (#771/#804); pass a custom URL or use the default. Read-only: "
                             "nothing is typed and no control is clicked, so publish reads UNKNOWN")
    parser.add_argument("--feed-sort", action="store_true",
                        help="report whether the home feed's 'Sort by -> Recent' control resolves "
                             "and whether the flip sticks (#817)")
    parser.add_argument("--watch", action="store_true",
                        help="request the watchable Grid debug node so the session is visible via "
                             "noVNC; falls back to the pool if the debug node is busy/absent")
    parser.add_argument("--reaction-probe", action="store_true",
                        help="capture the feed cards' reaction controls (issue #816). Read-only: "
                             "it never leaves a reaction.")
    parser.add_argument("--reaction-cards", type=int, default=3,
                        help="how many feed cards to sample for --reaction-probe")
    parser.add_argument("--reaction-open-menu", action="store_true",
                        help="also hover and open the reaction fly-out to capture its option "
                             "labels. Changes no persisted state; the options are never clicked.")
    args = parser.parse_args(argv)

    if not (args.post_url or args.probe_composer or args.comment_outcome_url or args.dm_thread_url
            or args.article_editor_url or args.feed_sort or args.reaction_probe):
        parser.error("nothing to probe — pass --post-url, --comment-outcome-url, --dm-thread-url, "
                     "--article-editor-url, --feed-sort, --reaction-probe and/or "
                     "--probe-composer")

    from cqc_lem.app.run_automation import get_current_profile
    from cqc_lem.utilities.selenium_util import quit_gracefully

    driver, _wait, _email, profile = get_current_profile(user_id=args.user_id,
                                                        session_name="Live Validation",
                                                        debug=args.watch)
    report = {"user_id": args.user_id}
    try:
        if args.post_url:
            report["document_render"] = probe_document_render(driver, args.post_url)
            report["post_stats"] = probe_post_stats(driver, args.post_url)
        if args.comment_outcome_url:
            from cqc_lem.app.run_automation import _profile_slug
            # The reader compares slugs EXACTLY, so accept either form here: a full profile URL or
            # a bare slug typed on the command line.
            raw = args.our_slug or str(getattr(profile, "profile_url", "") or "")
            slug = _profile_slug(raw) or raw.strip().strip("/").lower()
            report["comment_outcome"] = probe_comment_outcome(driver, args.comment_outcome_url,
                                                              slug, args.comment_text)
        if args.dm_thread_url:
            from cqc_lem.utilities.linkedin.message_thread import resolve_self_name
            report["message_thread"] = probe_message_thread(
                driver, args.dm_thread_url, args.dm_thread_name,
                self_name=resolve_self_name(args.user_id, profile))
        if args.feed_sort:
            report["feed_sort"] = probe_feed_sort(driver)
        if args.probe_composer:
            report["composer"] = probe_composer(driver)
        if args.article_editor_url:
            report["article_editor"] = probe_article_editor(driver, args.article_editor_url)
        if args.reaction_probe:
            report["feed_reactions"] = probe_feed_reactions(
                driver, max_cards=args.reaction_cards, open_menu=args.reaction_open_menu)
    finally:
        quit_gracefully(driver)

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
