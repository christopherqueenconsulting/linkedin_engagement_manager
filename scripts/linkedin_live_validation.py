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

**Read-only.** It navigates and reads: it publishes nothing, comments on nothing, sends no
invites or DMs and changes no settings. ``--probe-composer`` additionally OPENS the post
composer to capture the "add a document" affordance's anchors and closes it with Escape without
attaching or posting anything.

Run it from inside a Selenium worker so the login/cookie/proxy stack is the production one
(``scripts/`` is not baked into the image, so pipe the file in on stdin, the same way
``weekly_linkedin_version_check.sh`` runs the version probe)::

    sudo docker exec -i celery_worker_selenium python - \
        --user-id 1 --post-url 'https://www.linkedin.com/feed/update/urn:li:activity:123/' \
        < scripts/linkedin_live_validation.py

The report is JSON on stdout — paste it into the issue. The parsing/verdict logic is pure and
unit-tested; the browser steps take injected callables so they are mocked in tests.
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
    args = parser.parse_args(argv)

    if not (args.post_url or args.probe_composer or args.comment_outcome_url or args.dm_thread_url
            or args.article_editor_url):
        parser.error("nothing to probe — pass --post-url, --comment-outcome-url, --dm-thread-url, "
                     "--article-editor-url and/or --probe-composer")

    from cqc_lem.app.run_automation import get_current_profile
    from cqc_lem.utilities.selenium_util import quit_gracefully

    driver, _wait, _email, profile = get_current_profile(user_id=args.user_id,
                                                        session_name="Live Validation")
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
        if args.probe_composer:
            report["composer"] = probe_composer(driver)
        if args.article_editor_url:
            report["article_editor"] = probe_article_editor(driver, args.article_editor_url)
    finally:
        quit_gracefully(driver)

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
