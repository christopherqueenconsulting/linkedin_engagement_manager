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
  6. On a profile's /details/experience/ page: what does the rebuilt experience parser actually
     read, and from which entity selector? — grounds #970. The whole profile is dumped into the
     voice-synthesis prompt, so a misparse here is not inert: it grounds every comment and DM.

**Read-only.** It navigates and reads: it publishes nothing, comments on nothing, sends no
invites or DMs and changes no settings. ``--probe-composer`` additionally OPENS the post
composer to capture the "add a document" affordance's anchors and closes it with Escape without
attaching or posting anything; ``--permalink-comment`` does the same for a POST's comment composer
(#966) — it clicks Comment so the box mounts, describes it, and Escapes, typing and submitting
nothing; ``--comment-outcome-url`` and ``--feed-sort`` flip a sort control, exactly as the
production readers they are grounding do.

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

Since issue #1013 every probe grades itself ``ok`` / ``drift`` / ``unknown`` (``state``) beside its
prose ``verdict``, ``--surfaces`` prints the Selenium surface → probe coverage matrix
(``docs/sdui-probe-coverage.md``), and ``--sweep`` runs every target-free probe in ONE session —
which is what ``scripts/weekly_sdui_drift_check.sh`` runs, filing ONE deduped ``agent:ready`` issue
per ``drift`` verdict. ``drift`` is claimed only against a page-native cross-check (a headline
count, the page's own empty-state copy, an anchor the chain should have matched); a page that did
not render is ``unknown`` and is never filed as a defect.
"""

import argparse
import json
import re
import sys
import time
from typing import Callable, Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

SIGNALS = ("reactions", "comments", "reposts", "impressions", "saves")
ANALYTICS_URL = "https://www.linkedin.com/analytics/post-summary/{urn}/"
FEED_URL = "https://www.linkedin.com/feed/"

# The report is fenced because this script's stdout is NOT only this script's: it runs inside the
# worker, and `cqc_lem.utilities.logger` writes to stdout too (`get_current_profile` alone emits
# "Getting Updated Profile" before the first probe). A human reading one report skims past those
# lines; the weekly cron pipes stdout into `json.loads`, which does not. Everything between the
# fences is the report and nothing else — see `scripts/sdui_drift_issues.py:load_sweep`.
REPORT_JSON_BEGIN = "===LEM-PROBE-JSON-BEGIN==="
REPORT_JSON_END = "===LEM-PROBE-JSON-END==="

# ─────────────────────────── the three-state verdict (issue #1013) ───────────────────────────
# Every probe grades itself into exactly one of these, next to its prose `verdict`. The prose is
# for a human reading one report; the state is what the weekly sweep files issues from, so the
# three have to stay decision-shaped and NOT interchangeable:
#
#   ok      — the locator chain resolved what production needs on this surface.
#   drift   — the PAGE shows content the locator cannot see. This is the only state that files an
#             issue, and it is only ever claimed against a page-native cross-check (a headline
#             count, the page's own empty-state copy, an anchor the chain should have matched).
#   unknown — the page did not render (auth wall, redirect, read raced the load) or the surface is
#             legitimately absent. Grounds nothing; re-run it. NEVER filed as a defect — three
#             separate lanes died silently because "nothing found" was read as "nothing there",
#             and reading "did not render" as drift is the same mistake pointed the other way.
STATE_OK = "ok"
STATE_DRIFT = "drift"
STATE_UNKNOWN = "unknown"
# Worst-wins ordering when a probe grades several sections (appreciation reads two surfaces) or the
# sweep summarizes many probes: a drift anywhere is the report's state.
_STATE_RANK = {STATE_OK: 0, STATE_UNKNOWN: 1, STATE_DRIFT: 2}


def worst_state(states) -> str:
    """The most severe of several states — drift beats unknown beats ok. Empty grades `unknown`:
    a sweep that measured nothing must not read as a clean bill of health."""
    ranked = [s for s in (states or []) if s in _STATE_RANK]
    if not ranked:
        return STATE_UNKNOWN
    return max(ranked, key=lambda s: _STATE_RANK[s])


def graded(reading: dict, state: str, verdict: str) -> dict:
    """Stamp a probe reading with its three-state grade and its prose verdict, in that order."""
    reading["state"] = state
    reading["verdict"] = verdict
    return reading


# ─────────────────────── surface inventory / probe coverage matrix (#1013) ───────────────────
# Every Selenium touchpoint in run_automation.py + utilities/linkedin/*, and the probe flag that
# grounds it. This is the machine-readable half of docs/sdui-probe-coverage.md — `--surfaces`
# prints it, a unit test asserts every `flag` is a real CLI argument and every `sweep` entry runs
# in the sweep, and another asserts the doc names every surface. A touchpoint with no probe row is
# a surface that can rot silently, which is exactly what this issue exists to end.
#
# `sweep`: runs in the weekly unattended sweep, which means it needs NO caller-supplied target
# (or resolves one itself from the user's own profile / DB) and is read-only enough to run
# unattended. Everything else needs a URL a human picks.
#
# `arg`: the value the flag takes, when it takes one. It rides into the sweep JSON so the drift
# filer can print a reproduce command that actually RUNS — a `--profile-scrape` with no URL after
# it is an argparse error, and an issue whose repro line does not run is an issue nobody reproduces.
SURFACES = (
    {"key": "feed_sort", "surface": "Home feed 'Sort by → Recent' control",
     "code": "run_automation._switch_feed_to_recent", "flag": "--feed-sort", "sweep": True},
    {"key": "feed_reactions", "surface": "Feed card walk + reaction controls",
     "code": "run_automation._card_for_textbox / react_to_post_inline",
     "flag": "--reaction-probe", "sweep": True},
    {"key": "composer", "surface": "Feed share-box composer",
     "code": "run_automation.auto_post_to_group (share box) / _post_composer_for_card",
     "flag": "--probe-composer", "sweep": True},
    {"key": "profile_views", "surface": "Profile-views analytics viewer list",
     "code": "run_automation._PROFILE_VIEWER_ROWS_JS", "flag": "--profile-views", "sweep": True},
    {"key": "profile_scrape", "surface": "Profile header scrape (name / headline / degree badge)",
     "code": "scrapper.parse_profile_header + run_automation._profile_is_first_degree",
     "flag": "--profile-scrape", "arg": "<profile-url>", "sweep": True},
    {"key": "connect_dialog", "surface": "Connect invite dialog (custom-invite URL route)",
     "code": "run_automation._open_connect_invite_dialog", "flag": "--connect-dialog",
     "arg": "<profile-url>", "sweep": False},
    {"key": "catchup_cards", "surface": "Catch-up moment cards",
     "code": "run_automation._CATCHUP_CARD_LOCATORS", "flag": "--catchup-cards", "sweep": True},
    {"key": "group_composer", "surface": "Group share box / post editor",
     "code": "run_automation.auto_post_to_group", "flag": "--group-composer",
     "arg": "<group-id>", "sweep": False},
    {"key": "company_invite", "surface": "Company-page invite modal (credits / invitees / Invite)",
     "code": "company_page_inviter.automate_invitations", "flag": "--company-invite",
     "sweep": True},
    {"key": "sent_invites", "surface": "Invitation manager → Sent (withdraw controls)",
     "code": "stale_invites.read_pending_invites", "flag": "--sent-invites", "sweep": True},
    {"key": "roster_follow", "surface": "Roster target's activity page Follow control",
     "code": "run_automation._resolve_follow_control", "flag": "--roster-follow",
     "arg": "<profile-url>", "sweep": False},
    {"key": "roster_connect", "surface": "Roster target's activity page connection state",
     "code": "run_automation._resolve_connect_state", "flag": "--roster-connect",
     "arg": "<profile-url>", "sweep": False},
    {"key": "appreciation_sources", "surface": "Recommendations received + mentions feed",
     "code": "run_automation._RECOMMENDATION_CARD_LOCATORS / _MENTION_CARD_LOCATORS",
     "flag": "--appreciation-sources", "sweep": True},
    {"key": "article_editor", "surface": "Newsletter/article editor publish steps",
     "code": "article_editor.find_article_editor_elements", "flag": "--article-editor-url",
     "arg": "<editor-url>", "sweep": True},
    {"key": "post_stats", "surface": "Own post detail + analytics counts",
     "code": "run_automation._post_social_counts", "flag": "--post-url", "arg": "<post-url>",
     "sweep": False},
    {"key": "document_render", "surface": "Published post media render (document vs image)",
     "code": "run_automation (media anchors)", "flag": "--post-url", "arg": "<post-url>",
     "sweep": False},
    {"key": "comment_outcome", "surface": "Comment thread + sort (demotion read)",
     "code": "run_automation._comment_items / _switch_comment_sort",
     "flag": "--comment-outcome-url", "arg": "<post-url>", "sweep": False},
    {"key": "message_thread", "surface": "Message-thread resolution ladder",
     "code": "message_thread.open_message_thread", "flag": "--dm-thread-url",
     "arg": "<profile-url>", "sweep": False},
    {"key": "permalink_comment", "surface": "Post permalink card → Comment → composer",
     "code": "run_automation._permalink_post_card / _post_composer_for_card",
     "flag": "--permalink-comment", "arg": "<post-url>", "sweep": False},
)

# The sweep runs these in ONE Chrome session, cheapest/safest first so a session that dies part-way
# still grounds the surfaces most likely to have rotted.
SWEEP_ORDER = tuple(s["key"] for s in SURFACES if s["sweep"])

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


def post_stats_state(reading: Optional[dict]) -> str:
    """Three-state grade for one stats read. The label LINES are the page-native cross-check: a
    signal the parser scored zero while the page renders its label is the parser reading a layout
    it no longer matches — drift. Neither counts nor labels means the page never rendered."""
    reading = dict(reading or {})
    signals = (reading.get("signals") or {}).values()
    lines = (reading.get("detail_lines") or []) + (reading.get("analytics_lines") or [])
    if any((s or {}).get("value") for s in signals):
        return STATE_OK
    if lines:
        return STATE_DRIFT
    return STATE_UNKNOWN


def post_stats_verdict(reading: Optional[dict]) -> str:
    state = post_stats_state(reading)
    if state == STATE_OK:
        return "counts read"
    if state == STATE_DRIFT:
        return ("the page renders count labels but the parser scored every signal 0 — layout drift; "
                "rewrite _post_social_counts from `detail_lines` / `analytics_lines`")
    return "no counts and no count labels — the post/analytics page did not render; re-run"


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

    reading = {"post_url": post_url, "activity_urn": urn,
               "signals": signal_sources(detail_counts, analytics_counts),
               "detail_lines": label_lines(detail_text),
               "analytics_lines": label_lines(analytics_text)}
    return graded(reading, post_stats_state(reading), post_stats_verdict(reading))


def probe_document_render(driver, post_url: str, sleep=time.sleep) -> dict:
    """C1: capture the media anchors a published post renders, so 'native document vs
    multi-image share' stops being an assumption."""
    driver.get(post_url)
    sleep(5)
    try:
        anchors = driver.execute_script(_MEDIA_JS, list(_MEDIA_TOKENS)) or []
    except Exception as e:
        return {"post_url": post_url, "verdict": "unknown", "state": STATE_UNKNOWN,
                "error": str(e), "anchors": []}
    verdict = media_verdict(anchors)
    # `verdict` stays the MEDIA KIND here (document/image/unknown) — callers and tests read it as
    # that. The three-state grade rides alongside: a post whose media nodes carry no recognizable
    # token is a render this probe cannot classify, never a proven drift.
    return {"post_url": post_url, "verdict": verdict,
            "state": STATE_OK if verdict != "unknown" else STATE_UNKNOWN,
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
        # A share box that did not resolve is only DRIFT if the feed itself rendered — otherwise
        # this run was signed out or redirected and grounds nothing.
        reading = {"opened": False, "controls": [], "document_affordance": None,
                   "page_text": page_text_sample(driver),
                   "visible_controls": visible_button_labels(driver)}
        if reading["page_text"]:
            return graded(reading, STATE_DRIFT,
                          "the feed rendered but no share-box control resolved — re-ground the "
                          "'Start a post' locator from `visible_controls`")
        return graded(reading, STATE_UNKNOWN,
                      "the feed did not render at all (signed out, redirected, or the read raced "
                      "the load) — this reading grounds nothing; re-run it")
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
    reading = {"opened": True, "controls": controls,
               "document_affordance": find_document_affordance(controls)}
    if not controls:
        return graded(reading, STATE_DRIFT,
                      "the composer opened but carries no readable control labels — its dialog "
                      "anchors have rotated")
    return graded(reading, STATE_OK, f"composer opened with {len(controls)} control(s)")


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


def comment_outcome_state(reading: Optional[dict]) -> str:
    """Three-state grade for one comment-outcome read. The rendered comment count is the
    page-native cross-check: a thread that rendered comments but yielded no sort control (or none
    of our own) is a reader that has lost the surface, while a thread that rendered NOTHING never
    grounded anything."""
    reading = dict(reading or {})
    rendered = int(reading.get("rendered_comments") or 0)
    if not rendered:
        return STATE_UNKNOWN
    if not reading.get("sort_control_found"):
        return STATE_DRIFT
    return STATE_OK


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
    return graded(reading, comment_outcome_state(reading), comment_outcome_verdict(reading))


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


def feed_sort_state(reading: Optional[dict]) -> str:
    """Three-state grade for one feed-sort read. `visible_controls` is the page-native cross-check:
    a feed that rendered controls but yielded no sort control among them is drift, while a screen
    with no controls at all is a feed that never rendered."""
    reading = dict(reading or {})
    if reading.get("sort_after") == SORT_RECENT:
        return STATE_OK
    if not reading.get("visible_controls"):
        return STATE_UNKNOWN
    if not reading.get("control_found") or reading.get("option_found") is False:
        return STATE_DRIFT
    return STATE_UNKNOWN


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
    return graded(reading, feed_sort_state(reading), feed_sort_verdict(reading))


def probe_profile_viewers(driver, sleep=time.sleep) -> dict:
    """Profile-views analytics page: report how many viewer rows the production locator matches,
    the caption forms in play, and the page's own headline count so a locator miss cannot read as
    an empty page. Read-only — nothing is clicked and nobody is engaged.

    The production locator is a TEXT discriminator (an /in/ anchor carrying a "Viewed …" line);
    `sample_rows` holds each row's text lines, which is exactly what the next locator gets
    re-grounded from when this rotates again."""
    from cqc_lem.app.run_automation import (_PROFILE_VIEWER_ROWS_JS, _PROFILE_VIEWER_SCROLL_JS,
                                            _PROFILE_VIEWER_STAT_JS)

    driver.get("https://www.linkedin.com/analytics/profile-views/")
    sleep(8)
    rows = driver.execute_script(_PROFILE_VIEWER_ROWS_JS) or []
    stat = driver.execute_script(_PROFILE_VIEWER_STAT_JS)
    driver.execute_script(_PROFILE_VIEWER_SCROLL_JS)
    sleep(3)
    rows_after_scroll = driver.execute_script(_PROFILE_VIEWER_ROWS_JS) or []
    reading = {
        "url": getattr(driver, "current_url", ""),
        "row_count": len(rows),
        "row_count_after_scroll": len(rows_after_scroll),
        "headline_stat": stat,
        "caption_forms": sorted({(r.get("viewed") or "").split(" ", 2)[-1] for r in rows if r.get("viewed")}),
        "sample_rows": [{"href": r.get("href"), "viewed": r.get("viewed")} for r in rows[:5]],
    }
    if stat and not rows:
        return graded(reading, STATE_DRIFT,
                      f"headline reports {stat} viewers but the row locator matched none — "
                      f"selector drift")
    if not rows:
        return graded(reading, STATE_UNKNOWN,
                      "no viewers listed and no headline stat — page empty or not rendered")
    if len(rows_after_scroll) <= len(rows) and stat and len(rows_after_scroll) < int(stat):
        # A scroll that grew nothing is only drift when the page's OWN headline says there is more
        # to load. An account with eight viewers really does have one page of them, and grading
        # that as drift would file the same issue every week for a healthy surface.
        return graded(reading, STATE_DRIFT,
                      f"{len(rows)} rows matched and the scroll did not grow the list, but the "
                      f"headline reports {stat} viewers — lazy-load drift")
    return graded(reading, STATE_OK,
                  f"{len(rows)} rows matched, scroll grew the list to {len(rows_after_scroll)}")


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


def message_thread_state(reading: Optional[dict]) -> str:
    """Three-state grade for one ladder walk. Six routes are tried, so a walk that opened NOTHING
    while the profile page rendered is drift; a walk that never reached a rendered page is unknown.
    A thread that opened but reads no message events is drift too — that is exactly the reply
    detection going quiet."""
    reading = dict(reading or {})
    if not reading.get("opened"):
        return STATE_DRIFT if reading.get("routes_tried") else STATE_UNKNOWN
    return STATE_OK if reading.get("events") else STATE_DRIFT


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


def page_text_sample(driver, limit: int = 600) -> str:
    """The screen's own words — what the page SAYS, next to what the probe could resolve on it.

    Control labels alone cannot tell a rendered-but-empty surface apart from one whose anchors
    rotated: both hand back nothing. A page that rendered says so in prose ("No pending
    invitations"), and a page that did not render says nothing at all. Best-effort by design — a
    probe must not lose its reading because one text read went stale."""
    for selector in ("main", "body"):
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                text = " ".join((element.text or "").split())
                if text:
                    return text[:limit]
        except Exception:
            continue
    return ""


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
        return graded(out, STATE_UNKNOWN, f"the feed could not be loaded ({out['error']}) — re-run")

    # Always record WHICH screen we landed on. A zero-card result is only actionable next to the
    # evidence of what was actually rendered — an auth wall and a drifted card selector look
    # identical in a bare `cards_found: 0`.
    try:
        out["url"] = str(driver.current_url or "")[:200]
        out["title"] = str(driver.title or "")[:120]
    except Exception:
        # Diagnostic metadata only — a session too dead to report its own URL still has
        # useful card evidence below, so losing this must not lose the run.
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

    # Call the SHIPPED card walk, not a copy of it. An inlined duplicate silently grounds whatever
    # the probe's author last pasted — this probe reported cards_found: 0 against a build that had
    # already fixed the walk, because the copy still carried the old aria-label-only JS.
    try:
        from cqc_lem.app.run_automation import _card_for_textbox as prod_card_for_textbox
        out["card_walk_source"] = "image"
    except Exception:
        prod_card_for_textbox = None
        out["card_walk_source"] = "probe-carried"

    def _walk(box):
        if prod_card_for_textbox is not None:
            return prod_card_for_textbox(driver, box)
        return driver.execute_script(
            "let el=arguments[0],d=0;while(el&&d<15){"
            "if(el.querySelector&&el.querySelector(\"button[aria-label='Comment']\"))return el;"
            "el=el.parentElement;d++;}return null;", box)

    try:
        boxes = driver.find_elements(By.CSS_SELECTOR, prod_sel)
        out["textboxes_found"] = len(boxes)
        cards = []
        for box in boxes:
            if len(cards) >= max_cards:
                break
            try:
                card = _walk(box)
            except Exception:
                card = None
            if card is not None:
                cards.append(card)
    except Exception as e:
        out["error"] = f"card enumeration failed: {type(e).__name__}: {e}"
        return graded(out, STATE_UNKNOWN, f"{out['error']} — re-run")
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
                # data-testid is optional provenance; a stale element must not drop the control
                # from the report, since its aria-label/text are the anchors we came for.
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
            # Best-effort context for a zero-card result. `visible_buttons` above is the
            # primary evidence; body text is a nice-to-have.
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
                    # Same as above: testid is extra provenance, never the reason to drop a control.
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
    return graded(out, feed_reactions_state(out), feed_reactions_verdict(out))


def feed_reactions_state(reading: Optional[dict]) -> str:
    """Three-state grade for one feed-card capture. The page's own controls are the cross-check:
    text boxes that resolved but walked to no card is the #964/#1012 shape (the DOM is there, the
    walk cannot see it), while a feed with no text boxes AND no buttons never rendered."""
    reading = dict(reading or {})
    if reading.get("error"):
        return STATE_UNKNOWN
    if reading.get("cards_found"):
        return STATE_OK if any((reading.get("anchors_present") or {}).values()) else STATE_DRIFT
    if reading.get("textboxes_found") or reading.get("comment_controls"):
        return STATE_DRIFT
    if reading.get("visible_buttons"):
        return STATE_DRIFT
    return STATE_UNKNOWN


def feed_reactions_verdict(reading: Optional[dict]) -> str:
    state = feed_reactions_state(reading)
    reading = dict(reading or {})
    if state == STATE_OK:
        return (f"{reading.get('cards_found')} card(s) walked, reaction anchors present: "
                f"{sorted(k for k, v in (reading.get('anchors_present') or {}).items() if v)}")
    if state == STATE_UNKNOWN:
        return ("the feed did not render (no post text nodes and no controls) — this reading "
                "grounds nothing; re-run it")
    if not reading.get("cards_found"):
        return (f"{reading.get('textboxes_found') or 0} post text node(s) resolved but the card "
                f"walk reached none of them — re-ground _card_for_textbox from `comment_controls`")
    return ("cards walked but not one carried a reaction anchor — re-ground react_to_post_inline "
            "from the per-card `controls`")


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
    return graded(verdict, article_editor_state(verdict), article_editor_reading(verdict))


def article_editor_state(verdict: Optional[dict]) -> str:
    """Three-state grade for one editor read. The screen's own buttons are the cross-check: an
    editor that rendered controls but resolved none of the publish steps is drift; a screen with no
    controls at all never rendered."""
    verdict = dict(verdict or {})
    if verdict.get("editor_ready"):
        return STATE_OK
    if not verdict.get("buttons"):
        return STATE_UNKNOWN
    return STATE_DRIFT


def permalink_comment_verdict(reading: dict) -> str:
    """What one permalink reading proves about the #966 comment path.

    The failure this exists to catch is SILENT: the pre-SDUI composer XPaths could only time out, so
    profile-viewer and outreach-funnel comments died with a log line saying they "might not have
    worked". Anything short of a resolved composer here means production posts nothing."""
    reading = dict(reading or {})
    if not reading.get("cards_found"):
        return ("no card carrying a comment action rendered — production logs a FAILURE and posts "
                "nothing (comments may be restricted on this post, or the card walk has drifted)")
    if not reading.get("card_matched_urn") and reading.get("permalink_urn"):
        if reading.get("top_card_urn"):
            return ("the top card belongs to a DIFFERENT post than the permalink — production "
                    "refuses to comment rather than comment on a recommendation")
        return ("no card claimed the permalink's URN and the top card's URN is unreadable — "
                "production falls back to the top card")
    if not reading.get("comment_action"):
        return "no Comment action on the resolved card — production cannot open a composer"
    if not reading.get("composer"):
        return ("Comment action clicked but no composer resolved for this card — production skips "
                "the post; this is the #966 failure mode returning")
    return "composer resolved on the permalink card — production would type and submit here"


def probe_permalink_comment(driver, post_url: str,
                            sleep: Callable[[float], None] = time.sleep) -> dict:
    """#966: open a post PERMALINK and report whether the comment path's card → comment action →
    composer chain resolves there — the surface `comment_on_post` runs on (profile-viewer engagement
    and the outreach funnel's COMMENT stage).

    Read-only in the sense `--probe-composer` already is: it clicks the post's own Comment button to
    make the composer mount, describes it, then presses Escape. Nothing is typed and nothing is
    submitted, so no comment is left. It never touches the reaction controls — `--reaction-probe`
    covers those."""
    from cqc_lem.app.run_automation import (_FEED_POST_TEXT_SEL, _COMMENT_ACTION_LOCATORS, _URN_RE,
                                            _card_for_textbox, _feed_post_urn_from_card,
                                            _permalink_post_card, _post_composer_for_card)
    from cqc_lem.utilities.selenium_util import click_first, find_first
    from selenium.webdriver import ActionChains
    from selenium.webdriver.support.ui import WebDriverWait

    wait = WebDriverWait(driver, 10)
    driver.get(post_url)
    sleep(5)

    cards = []
    for box in driver.find_elements(By.CSS_SELECTOR, _FEED_POST_TEXT_SEL):
        try:
            card = _card_for_textbox(driver, box)
        except Exception:
            continue
        if card is not None:
            cards.append(card)

    m = _URN_RE.search(post_url or "")
    wanted = m.group(0).lower() if m else None
    chosen = _permalink_post_card(driver, post_url)
    reading = {"post_url": post_url,
               "url": getattr(driver, "current_url", post_url),
               "permalink_urn": wanted,
               # More than one is EXPECTED on a permalink page — LinkedIn stacks recommendations
               # under the post. This count is what makes the URN match worth having.
               "cards_found": len(cards),
               "top_card_urn": _feed_post_urn_from_card(cards[0], driver=driver) if cards else None,
               "card_resolved": chosen is not None,
               "card_matched_urn": bool(chosen is not None and wanted
                                        and _feed_post_urn_from_card(chosen, driver=driver) == wanted),
               "comment_action": None,
               "composer": None,
               # The live labels a re-grounding pass would be written from — a bare "not found" is
               # not re-groundable.
               "visible_controls": visible_button_labels(driver)}

    if chosen is not None:
        action = find_first(driver, wait, _COMMENT_ACTION_LOCATORS, "Comment action",
                            parent_element=chosen, required=False, visible_only=True, max_try=1)
        reading["comment_action"] = element_evidence(action) if action is not None else None
        if action is not None:
            click_first(driver, wait, _COMMENT_ACTION_LOCATORS, "Open comment composer",
                        parent_element=chosen, required=False, max_try=1)
            sleep(2)
            composer = _post_composer_for_card(driver, chosen)
            reading["composer"] = element_evidence(composer) if composer is not None else None
            try:
                ActionChains(driver).send_keys(Keys.ESCAPE).perform()
            except Exception:
                # Closing is courtesy only — nothing was typed, the driver is quit in main(), and a
                # failed Escape must not mask the evidence this probe exists to report.
                pass

    reading["verdict"] = permalink_comment_verdict(reading)
    return reading


def roster_follow_verdict(reading: dict) -> str:
    """What one activity-page reading proves about the #962 follow lane.

    'unknown' is the finding that matters: production treats it as "do nothing", so a run that keeps
    reporting unknown on accounts you know you do not follow means the owner-named control has
    rotated and the lane has gone quiet — not that the roster is fully followed."""
    reading = dict(reading or {})
    state = reading.get("follow_state")
    if state == "following":
        return "already following — production would record this WITHOUT a click"
    if state == "not_following":
        label = (reading.get("control") or {}).get("aria_label") or (reading.get("control") or {}).get("text")
        return f"Follow control resolved ({label or '?'}) — a click would be attempted"
    if not reading.get("owner_name"):
        return ("page owner's name unreadable from the <title>, so there is nothing to anchor the "
                "label match on — production returns unknown and clicks nothing")
    if not reading.get("posts_on_page"):
        return ("no posts rendered — the page may not have loaded — production returns unknown "
                "and clicks nothing")
    return ("no control naming the page owner resolved — production returns unknown and clicks "
            "nothing")


def roster_follow_state(reading: Optional[dict]) -> str:
    """Three-state grade for one activity-page read. The posts on the page are the cross-check: a
    page that rendered the owner's activity but yielded no follow control is drift, while a page
    that rendered neither posts nor an owner name never loaded."""
    reading = dict(reading or {})
    if reading.get("follow_state") in ("following", "not_following"):
        return STATE_OK
    if not reading.get("owner_name") and not reading.get("posts_on_page"):
        return STATE_UNKNOWN
    if not reading.get("posts_on_page"):
        return STATE_UNKNOWN
    return STATE_DRIFT


def probe_roster_follow(driver, profile_url: str,
                        sleep: Callable[[float], None] = time.sleep) -> dict:
    """#962: open a roster target's recent-activity page and report whether the top-card follow
    control resolves, and which state it reads.

    STRICTLY read-only — it resolves the control and describes it. Nothing is clicked, so no account
    is followed by running this. That is the point: the clicker must not ship before a live run has
    shown that this resolver finds the right button on the real page."""
    from cqc_lem.app.run_automation import (_FEED_POST_TEXT_SEL, _activity_page_owner_name,
                                            _resolve_follow_control, _roster_activity_url)

    url = _roster_activity_url(profile_url)
    driver.get(url)
    sleep(5)
    state, control = _resolve_follow_control(driver, profile_url)
    try:
        posts = len([b for b in driver.find_elements(By.CSS_SELECTOR, _FEED_POST_TEXT_SEL)
                     if b.is_displayed()])
    except Exception:
        posts = 0
    reading = {"profile_url": profile_url,
               "activity_url": url,
               "url": getattr(driver, "current_url", url),
               # `follow_state`, not `state`: every probe's `state` is now the three-state drift
               # grade (#1013), and the resolver's own following/not_following/unknown is a
               # different question that must not be collapsed into it.
               "follow_state": str(state),
               # The name the label match anchored on — the resolver only accepts controls that
               # carry it, so a wrong or empty name here explains an unknown above.
               "owner_name": _activity_page_owner_name(driver),
               "control": element_evidence(control) if control is not None else None,
               "posts_on_page": posts,
               # The live labels the next locator pass would be written from — a bare "not found"
               # is not re-groundable.
               "visible_controls": visible_button_labels(driver)}
    return graded(reading, roster_follow_state(reading), roster_follow_verdict(reading))


def appreciation_verdict(reading: dict) -> str:
    """What one reading proves about an appreciation source (#968).

    'cards but nothing dated' is the finding that matters: production skips an undated card rather
    than thank a five-year-old recommendation, so a section that renders and never dates reads as
    "no recent recommendations" forever — a silently dead trigger, not an empty one."""
    reading = dict(reading or {})
    cards = int(reading.get("cards") or 0)
    if not cards:
        return ("no cards resolved — either the surface is genuinely empty or the card locator "
                "chain has rotated; production sends nothing either way")
    if not reading.get("dated"):
        return (f"{cards} card(s) resolved but none carried a readable date — production skips "
                f"every one of them, so this trigger is silently dead")
    people = reading.get("people") or []
    return (f"{cards} card(s), {reading.get('dated')} dated, {len(people)} inside the "
            f"{reading.get('lookback_days')}-day window — production would thank those")


def appreciation_state(reading: Optional[dict]) -> str:
    """Three-state grade for ONE appreciation surface. Cards that resolved but never date is the
    silently-dead trigger this probe exists to catch — drift. No cards at all cannot be told apart
    from an empty section, so it grades unknown and is never filed as a defect."""
    reading = dict(reading or {})
    if not int(reading.get("cards") or 0):
        return STATE_UNKNOWN
    return STATE_OK if reading.get("dated") else STATE_DRIFT


def probe_appreciation_sources(driver, user_id: int, profile_url: str = "",
                               sleep: Callable[[float], None] = time.sleep) -> dict:
    """#968: read the two standing surfaces the recommendation + collaboration appreciation DMs are
    scraped from, and report what production would make of them.

    STRICTLY read-only, and it deliberately does NOT call the production scrapers — those are gated
    OFF by `APPRECIATION_SOURCES_ENABLED` precisely until this probe has grounded them, so the probe
    drives the same locator chains and the same date parsers directly. Nothing is messaged: the
    ledger is only READ (`has_appreciation_touch`), never claimed."""
    from cqc_lem.app.run_automation import (_MENTIONS_URL, _MENTION_ACTOR_LOCATORS,
                                            _MENTION_CARD_LOCATORS, _MENTION_TEXT_RE,
                                            _RECOMMENDATION_AUTHOR_LOCATORS,
                                            _RECOMMENDATION_CARD_LOCATORS, _card_person, _card_text,
                                            _mention_actor_name, _own_profile_url,
                                            _parse_recommendation_date, _parse_relative_age_days,
                                            appreciation_lookback_days)
    from cqc_lem.utilities.db import has_appreciation_touch
    from cqc_lem.utilities.selenium_util import find_all_first

    lookback = appreciation_lookback_days()

    def _read(url: str, card_locators, person_locators, age_of, event_type: str,
              require=None, name_of=None) -> dict:
        driver.get(url)
        sleep(5)
        cards = find_all_first(driver, card_locators)
        rows, dated = [], 0
        for card in cards:
            text = _card_text(card)
            if require is not None and not require(text):
                continue
            age_days = age_of(text)
            if age_days is not None:
                dated += 1
            person_url, name = _card_person(card, person_locators)
            # Same fallback production uses, so a blank `name` here means the DM really would say
            # "Hi there" — not that the probe read the card differently.
            if not name and name_of is not None:
                name = name_of(text)
            rows.append({"profile_url": person_url, "name": name,
                         "age_days": None if age_days is None else round(age_days, 2),
                         "in_window": age_days is not None and age_days <= lookback,
                         # Already thanked = production skips it even when everything else resolves.
                         "already_thanked": bool(person_url) and has_appreciation_touch(
                             user_id, person_url, event_type),
                         "text": (text or "")[:160]})
        reading = {"url": url, "current_url": getattr(driver, "current_url", url),
                   "lookback_days": lookback, "cards": len(rows), "dated": dated,
                   "people": [r for r in rows if r["in_window"] and r["profile_url"]
                              and not r["already_thanked"]],
                   "rows": rows[:10]}
        return graded(reading, appreciation_state(reading), appreciation_verdict(reading))

    own = _own_profile_url(driver, user_id) if not profile_url else profile_url.rstrip("/")
    report = {"own_profile_url": own}
    if own:
        report["recommendations_received"] = _read(
            f"{own}/details/recommendations/", _RECOMMENDATION_CARD_LOCATORS,
            _RECOMMENDATION_AUTHOR_LOCATORS, _parse_recommendation_date, "recommendation_received")
    else:
        report["recommendations_received"] = {
            "state": STATE_UNKNOWN,
            "verdict": "own profile URL unresolved — production skips the recommendations scan"}
    report["mentions"] = _read(_MENTIONS_URL, _MENTION_CARD_LOCATORS, _MENTION_ACTOR_LOCATORS,
                               _parse_relative_age_days, "collaboration",
                               require=lambda t: bool(_MENTION_TEXT_RE.search(t or "")),
                               name_of=_mention_actor_name)
    # Two surfaces, one report: worst-wins, so a dead recommendations trigger is not hidden by a
    # healthy mentions feed.
    sections = (report["recommendations_received"], report["mentions"])
    return graded(report, worst_state([s.get("state") for s in sections]),
                  " | ".join(f"{name}: {section.get('verdict')}"
                             for name, section in (("recommendations", sections[0]),
                                                   ("mentions", sections[1]))))


# LinkedIn's own "there is nothing here" copy on the sent-invitation manager. Matching one of these
# is what tells an EMPTY account apart from rotated row anchors: both resolve zero rows, so without
# the page's own words a clean account and a dead selector produce the same report — which is the
# reading the first live run of this probe came back with (PR #983).
#
# Every alternative carries its OWN negation, deliberately: a pattern loose enough to match "You have
# 3 pending invitations" would report an empty state on a page that is full of rows the anchors
# missed — a false clean bill of health on exactly the drift this probe exists to catch.
_SENT_EMPTY_STATE_RE = re.compile(
    r"\bno (?:pending |sent |new )?invitations?\b"
    r"|\bhaven'?t sent any (?:pending )?invitations?\b"
    r"|\bnothing to see here\b", re.IGNORECASE)


def sent_invite_empty_state(page_text: Optional[str]) -> Optional[str]:
    """The empty-state phrase the page rendered, or `None`.

    `None` is not "the account has invites" — it is "the page never said it was empty", which is
    exactly the case that must NOT be read as a clean account."""
    match = _SENT_EMPTY_STATE_RE.search(str(page_text or ""))
    return match.group(0).strip() if match else None


def sent_invites_verdict(reading: dict) -> str:
    """What one invitation-manager reading proves about the #969 withdrawal lane.

    The lane ships OFF and cannot be turned on honestly until this says rows resolved AND their sent
    dates parsed: production skips every row it cannot age, so "0 rows" and "rows, none dated" both
    mean the lane would run every night and withdraw nothing — which is the stub it replaced.

    Zero rows has THREE readings and they are not interchangeable — an empty account, rotated
    anchors, and a page that never rendered. The page's own text is what separates them; a report
    that cannot tell them apart grounds nothing."""
    reading = dict(reading or {})
    rows = int(reading.get("rows_seen") or 0)
    if not rows:
        empty_state = reading.get("empty_state")
        if empty_state:
            return (f"no rows, and the page rendered its own empty state ({empty_state!r}) — this "
                    f"account has nothing outstanding. The row anchors are UNTESTED, not proven "
                    f"broken: re-probe once an invite is actually pending, before enabling the lane")
        if not str(reading.get("page_text") or "").strip():
            return ("no rows AND no page text — the invitation manager did not render at all "
                    "(signed out, redirected, or the read raced the load). This reading grounds "
                    "nothing; re-run it")
        return ("no pending-invite rows resolved and no empty-state copy on the page — either this "
                "account has none outstanding, or the row anchors (a Withdraw control above an "
                "/in/ link) have moved. Check `page_text` / `visible_controls` against the page")
    dated = int(reading.get("dated") or 0)
    if not dated:
        return (f"{rows} row(s) resolved but NOT ONE carried a readable 'Sent ... ago' stamp — "
                f"production ages nothing and withdraws nothing. `sample_text` is what the next "
                f"parser pass should be written from")
    stale = int(reading.get("stale_at_threshold") or 0)
    return (f"{rows} row(s) resolved, {dated} dated, {stale} at/over the {reading.get('threshold_days')}"
            f"-day threshold — production would attempt that many (bounded by the daily budget)")


def sent_invites_state(reading: Optional[dict]) -> str:
    """Three-state grade for one invitation-manager read. Zero rows has THREE readings and the
    page's own words are what separate them: its rendered empty state means the account really has
    nothing outstanding (ok), no text at all means the page never rendered (unknown), and a page
    full of prose that yielded no rows is the anchors having moved (drift)."""
    reading = dict(reading or {})
    rows = int(reading.get("rows_seen") or 0)
    if rows:
        return STATE_OK if int(reading.get("dated") or 0) else STATE_DRIFT
    if reading.get("empty_state"):
        return STATE_OK
    if not str(reading.get("page_text") or "").strip():
        return STATE_UNKNOWN
    return STATE_DRIFT


def probe_sent_invites(driver, threshold_days: Optional[int] = None,
                       sleep: Callable[[float], None] = time.sleep) -> dict:
    """#969: open the sent-invitation manager and report which pending rows resolve, what their
    "Sent ... ago" stamps parse to, and how many the threshold would call stale.

    STRICTLY read-only — it resolves rows and describes them. **Nothing is withdrawn by running
    this**, which is the point: withdrawing is one-way (LinkedIn blocks re-inviting the same person
    for weeks), so the clicker must not be switched on before a live run has shown that these
    anchors find the real rows and that their dates parse.

    It also samples the page's own text, because zero rows is otherwise undecidable: an account with
    nothing outstanding and a rotated row anchor report the same zero, and only the rendered empty
    state says which one this run looked at."""
    from cqc_lem.utilities.linkedin.stale_invites import (SENT_INVITATIONS_URL, _load_more_rows,
                                                          read_pending_invites, stale_after_days)

    threshold = int(threshold_days if threshold_days is not None else stale_after_days())
    driver.get(SENT_INVITATIONS_URL)
    sleep(4)
    expansions = _load_more_rows(driver, sleep=sleep)
    invites = read_pending_invites(driver)
    dated = [i for i in invites if i["age_days"] is not None]
    reading = {"url": getattr(driver, "current_url", SENT_INVITATIONS_URL),
               "threshold_days": threshold,
               "expansions": expansions,
               "rows_seen": len(invites),
               "dated": len(dated),
               "unreadable": len(invites) - len(dated),
               "stale_at_threshold": len([i for i in dated if i["age_days"] >= threshold]),
               "oldest_days": max((i["age_days"] for i in dated), default=None),
               # The rows as production read them, plus the raw card text a re-grounding pass would
               # rewrite the parser from. A bare "not found" is not re-groundable.
               "rows": [{"name": i["name"], "profile_url": i["profile_url"],
                         "age_days": i["age_days"],
                         "control": element_evidence(i["control"]) if i["control"] is not None
                         else None}
                        for i in invites[:10]],
               "sample_text": [i["text"][:240] for i in invites[:3]],
               # The page's own words. Zero rows is ambiguous without them — an account with nothing
               # outstanding and a rotated row anchor both report zero — and the empty state is the
               # only thing on the page that says which one this run is looking at.
               "page_text": page_text_sample(driver),
               "visible_controls": visible_button_labels(driver)}
    reading["empty_state"] = sent_invite_empty_state(reading["page_text"])
    return graded(reading, sent_invites_state(reading), sent_invites_verdict(reading))


# ─────────────────────────── connect invite dialog (#1012 / #1013) ───────────────────────────
# The rail hazard, as a pure function. Every "Invite <someone> to connect" control on a profile
# page is enumerated and attributed: the ones naming somebody other than the target are the
# "More profiles for you" rail, and clicking one sends a connection request to a stranger.
_INVITE_LABEL_RE = re.compile(r"invite\s+(.+?)\s+to\s+connect", re.IGNORECASE)
# What the profile says when there is already an invite outstanding — no dialog renders for those,
# which is an EXPECTED outcome and must not be graded as the locators having rotated.
_CONNECT_PENDING_RE = re.compile(r"\bpending\b|\binvitation sent\b|\bwithdraw invitation\b",
                                 re.IGNORECASE)


def _norm_person(name: Optional[str]) -> str:
    return " ".join(re.sub(r"[^a-z ]+", " ", str(name or "").lower()).split())


def invite_control_names(labels) -> list:
    """The person each 'Invite … to connect' control names, in order."""
    out = []
    for label in labels or []:
        match = _INVITE_LABEL_RE.search(str(label or ""))
        if match:
            out.append(match.group(1).strip())
    return out


def rail_invite_hazards(labels, target_name: str = "") -> list:
    """The invite controls that name someone OTHER than the target — the #1012 wrong-person hazard.

    With no target name, EVERY invite control is a hazard: a control we cannot attribute is
    precisely the one production must never click."""
    target = _norm_person(target_name)
    hazards = []
    for named in invite_control_names(labels):
        person = _norm_person(named)
        if not target or not (person in target or target in person):
            hazards.append(named)
    return hazards


def connect_dialog_state(reading: Optional[dict]) -> str:
    """Three-state grade for one connect-dialog read. The page's own words are the cross-check: a
    profile that rendered and offers no dialog is drift UNLESS it says an invite is already pending
    — that profile simply cannot ground this route, and grading it drift would file an issue for
    working behaviour."""
    reading = dict(reading or {})
    if reading.get("dialog_present"):
        return STATE_OK
    if not str(reading.get("page_text") or "").strip():
        return STATE_UNKNOWN
    if reading.get("invite_pending"):
        return STATE_UNKNOWN
    return STATE_DRIFT


def connect_dialog_verdict(reading: Optional[dict]) -> str:
    reading = dict(reading or {})
    hazards = reading.get("rail_hazards") or []
    tail = (f" WARNING: {len(hazards)} 'Invite … to connect' control(s) on this page name someone "
            f"else ({hazards[:3]}) — never click one." if hazards else "")
    state = connect_dialog_state(reading)
    if state == STATE_OK:
        # An absent note affordance is NOT drift on its own — a spent personalized-invite quota
        # hides it, and production treats that as the expected bare-invite fallback (#1039). It is
        # reported here because this probe is the only place the two readings can be told apart.
        # `is False` deliberately: a reading that never looked (None) must claim nothing.
        note = (" NOTE: no Add-a-note control on this dialog — production sends the invite bare and "
                "logs that at DEBUG (#1039); read it as selector rot only if this account still has "
                "personalized invites left."
                if reading.get("note_affordance_present") is False else "")
        return f"the custom-invite URL rendered the Connect dialog's own controls.{note}{tail}"
    if reading.get("invite_pending"):
        return (f"an invite is already pending for this profile, so no dialog renders — this "
                f"reading grounds nothing; probe a profile with no outstanding invite.{tail}")
    if not str(reading.get("page_text") or "").strip():
        return f"the page did not render at all — re-run.{tail}"
    return (f"the page rendered but no Connect dialog control resolved — re-ground "
            f"_CONNECT_DIALOG_LOCATORS from `visible_controls`.{tail}")


def probe_connect_dialog(driver, profile_url: str, sleep=time.sleep) -> dict:
    """#1012/#1013: navigate the custom-invite URL for `profile_url` and report whether the Connect
    dialog's own controls render, plus every 'Invite … to connect' control the PROFILE page carries
    and which of them name somebody else.

    STRICTLY read-only — it never clicks Send, never clicks an Invite control, and never opens the
    More menu. That is the whole point: this surface's last drift sent ~20 connection requests to
    strangers, so the probe that grounds it must be incapable of sending one."""
    from cqc_lem.app.run_automation import (_CONNECT_BARE_SEND_LOCATORS, _CONNECT_DIALOG_LOCATORS,
                                            _CONNECT_INVITE_URL, _CONNECT_NOTE_BUTTON_LOCATORS,
                                            _PROFILE_MORE_MENU_LOCATORS, _profile_slug)
    from cqc_lem.utilities.selenium_util import find_first
    from selenium.webdriver.support.ui import WebDriverWait

    wait = WebDriverWait(driver, 10)
    slug = _profile_slug(profile_url)
    reading = {"profile_url": profile_url, "slug": slug,
               "invite_url": _CONNECT_INVITE_URL.format(slug=slug) if slug else None}
    if not slug:
        return graded(reading, STATE_UNKNOWN,
                      "no /in/<slug> in the profile URL — production skips the URL route entirely")

    driver.get(reading["invite_url"])
    sleep(5)
    dialog = find_first(driver, wait, _CONNECT_DIALOG_LOCATORS, "Connect invite dialog",
                        required=False, warn_on_miss=False, max_try=1, visible_only=True)
    # Which of the dialog's two controls rendered is what tells a spent personalized-invite quota
    # apart from a rotated note selector — the distinction production cannot make (#1039). Only
    # asked when a dialog actually rendered: on a page with no dialog at all both would come back
    # absent, and "no Add-a-note control" is then a note-affordance reading nobody took. None, not
    # False — and two waits this probe does not have to sit through.
    note_present = bare_send_present = None
    if dialog is not None:
        note_present = find_first(driver, wait, _CONNECT_NOTE_BUTTON_LOCATORS, "Add a note button",
                                  required=False, warn_on_miss=False, max_try=1,
                                  visible_only=True) is not None
        bare_send_present = find_first(driver, wait, _CONNECT_BARE_SEND_LOCATORS,
                                       "Send without a note", required=False, warn_on_miss=False,
                                       max_try=1, visible_only=True) is not None
    reading.update({"url": getattr(driver, "current_url", ""),
                    "dialog_present": dialog is not None,
                    "dialog_control": element_evidence(dialog) if dialog is not None else None,
                    "note_affordance_present": note_present,
                    "bare_send_present": bare_send_present,
                    "page_text": page_text_sample(driver),
                    "visible_controls": visible_button_labels(driver)})

    # Then the profile page itself — the rail hazard only exists there, and the More-menu route is
    # production's fallback, so its trigger is RESOLVED (never clicked) as evidence it still exists.
    driver.get(profile_url)
    sleep(5)
    profile_controls = visible_button_labels(driver)
    more_menu = find_first(driver, wait, _PROFILE_MORE_MENU_LOCATORS, "Profile More menu",
                           required=False, warn_on_miss=False, max_try=1, visible_only=True)
    profile_text = page_text_sample(driver)
    reading.update({"profile_controls": profile_controls,
                    "more_menu_present": more_menu is not None,
                    "invite_controls": invite_control_names(profile_controls),
                    "rail_hazards": rail_invite_hazards(profile_controls, _page_owner_name(driver)),
                    "invite_pending": bool(_CONNECT_PENDING_RE.search(profile_text or ""))})
    reading["page_text"] = reading["page_text"] or profile_text
    return graded(reading, connect_dialog_state(reading), connect_dialog_verdict(reading))


def _page_owner_name(driver) -> str:
    """The profile's own display name from the <title> — the only thing an Invite control's label
    can honestly be attributed against."""
    try:
        title = str(driver.title or "")
    except Exception:
        return ""
    title = re.sub(r"^\(\d+\+?\)\s*", "", title)
    return re.sub(r"\s*[|\-–—]\s*LinkedIn.*$", "", title, flags=re.IGNORECASE).strip()


# ─────────────────────────────── profile header scrape (#1013) ───────────────────────────────
# The degree badge, page-natively. `_PROFILE_DEGREE_LOCATORS` is class-based (`span.dist-value` /
# `span.distance-badge`) and was confirmed dead on 2026-08-03, so the cross-check has to come from
# the page's own words: the top card still writes the degree as TEXT.
_DEGREE_TEXT_RE = re.compile(r"\b(1st|2nd|3rd)\b", re.IGNORECASE)

# Candidate re-grounding anchors: leaf nodes under <main> whose whole text IS a degree badge.
_DEGREE_ANCHOR_JS = """
const out = [];
const root = document.querySelector('main') || document.body;
for (const el of root.querySelectorAll('span,div,p,li')) {
  if (el.children.length) { continue; }
  const t = (el.textContent || '').trim();
  if (/^(1st|2nd|3rd)(\\s*\\+?\\s*degree.*)?$/i.test(t)) {
    out.push({tag: el.tagName.toLowerCase(), cls: el.getAttribute('class') || '',
              testid: el.getAttribute('data-testid') || '',
              aria: el.getAttribute('aria-label') || '', text: t});
  }
  if (out.length >= 8) { break; }
}
return out;
"""


def profile_scrape_state(reading: Optional[dict]) -> str:
    """Three-state grade for one profile-header read. The page's own degree TEXT is the
    cross-check: a badge the page writes and the locator chain cannot see is exactly the drift that
    made `_profile_is_first_degree` blind (and, through `profile.is_1st_connection`, made every
    profile viewer look like a non-connection)."""
    reading = dict(reading or {})
    if not str(reading.get("page_text") or "").strip():
        return STATE_UNKNOWN
    if not reading.get("full_name"):
        return STATE_DRIFT
    if reading.get("page_degree_token") and not reading.get("degree_locator_matches"):
        return STATE_DRIFT
    return STATE_OK


def profile_scrape_verdict(reading: Optional[dict]) -> str:
    reading = dict(reading or {})
    state = profile_scrape_state(reading)
    if state == STATE_UNKNOWN:
        return "the profile page did not render (auth wall, challenge, or redirect) — re-run"
    if not reading.get("full_name"):
        return (f"the page rendered but parse_profile_header found no name "
                f"({reading.get('parse_error') or 'no <h1> and no usable <title>'}) — every "
                f"profile-driven lane is blind here")
    if reading.get("page_degree_token") and not reading.get("degree_locator_matches"):
        return (f"name reads OK but the page shows a '{reading.get('page_degree_token')}' degree "
                f"badge that _PROFILE_DEGREE_LOCATORS matched none of — invites run against "
                f"existing connections and every profile viewer reads as a non-connection. "
                f"Re-ground from `degree_anchors`")
    if not reading.get("degree_grounded"):
        return (f"name '{reading.get('full_name')}' reads OK, but this profile carries no degree "
                f"badge at all (your own profile, or a page that renders none) — the degree half "
                f"is UNGROUNDED by this run; re-probe with a 2nd/3rd-degree profile URL")
    return (f"name '{reading.get('full_name')}' and degree "
            f"'{reading.get('connection') or reading.get('page_degree_token') or 'n/a'}' both read")


def probe_profile_scrape(driver, profile_url: str, sleep=time.sleep) -> dict:
    """#1013 (+ the owner's #1013 comment): run the SHIPPED profile-header parser and the SHIPPED
    degree-badge locator chain against a real profile, and cross-check both against what the page
    itself says. Read-only — it navigates and reads.

    `degree_anchors` is the point: `span.dist-value` / `span.distance-badge` are class anchors and
    CLAUDE.md says class anchors are gone, so this hands back the leaf nodes that DO carry the
    badge today, which is what the replacement chain gets written from."""
    from cqc_lem.app.run_automation import _PROFILE_DEGREE_LOCATORS
    from cqc_lem.utilities.linkedin.scrapper import ProfileUnavailableError, parse_profile_header
    from bs4 import BeautifulSoup

    driver.get(profile_url)
    sleep(6)
    reading = {"profile_url": profile_url, "url": getattr(driver, "current_url", profile_url),
               "page_text": page_text_sample(driver)}

    try:
        parsed = parse_profile_header(BeautifulSoup(driver.page_source, "html.parser"), profile_url)
    except ProfileUnavailableError as e:
        parsed, reading["parse_error"] = {}, str(e)[:200]
    except Exception as e:
        parsed, reading["parse_error"] = {}, f"{type(e).__name__}: {e}"[:200]
    reading.update({"full_name": parsed.get("full_name") or "",
                    "job_title": parsed.get("job_title") or "",
                    "connection": parsed.get("connection") or ""})

    matches = []
    for by, selector in _PROFILE_DEGREE_LOCATORS:
        try:
            texts = [(e.text or "").strip() for e in driver.find_elements(by, selector)]
        except Exception as e:
            texts = [f"<{type(e).__name__}>"]
        if texts:
            matches.append({"locator": f"{by}={selector}", "texts": texts[:5]})
    reading["degree_locator_matches"] = matches

    token = _DEGREE_TEXT_RE.search(reading["page_text"] or "")
    reading["page_degree_token"] = token.group(1) if token else None
    # Neither the page nor the locators showed a badge: this run says NOTHING about the degree
    # read. Reported explicitly so a sweep against the user's own profile (which carries no badge)
    # cannot be mistaken for a clean bill of health on the read that #1012 found blind.
    reading["degree_grounded"] = bool(reading["page_degree_token"] or matches)
    try:
        reading["degree_anchors"] = driver.execute_script(_DEGREE_ANCHOR_JS) or []
    except Exception as e:
        reading["degree_anchors"] = [{"error": f"{type(e).__name__}: {e}"}]
    try:
        reading["activity_links"] = len(driver.find_elements(
            By.CSS_SELECTOR, "a[href*='/recent-activity/']"))
    except Exception:
        # Provenance only — the name/degree reads above are what this probe grades on.
        reading["activity_links"] = 0
    return graded(reading, profile_scrape_state(reading), profile_scrape_verdict(reading))


# ───────────────────────────────── catch-up moment cards (#964) ──────────────────────────────
def catchup_state(reading: Optional[dict]) -> str:
    """Three-state grade for one catch-up read. The page's own `/in/` anchor count is the
    cross-check, and it is exactly the reading #964 lacked: the chain matched zero cards on a feed
    visibly showing ten, and `no_moments` was logged daily as if the feed were empty."""
    reading = dict(reading or {})
    if reading.get("cards_matched"):
        return STATE_OK
    if not str(reading.get("page_text") or "").strip():
        return STATE_UNKNOWN
    if reading.get("profile_anchors"):
        return STATE_DRIFT
    return STATE_UNKNOWN


def catchup_verdict(reading: Optional[dict]) -> str:
    reading = dict(reading or {})
    state = catchup_state(reading)
    if state == STATE_OK:
        return (f"{reading.get('cards_matched')} card(s) matched via "
                f"{reading.get('winning_locator')}, {reading.get('classified')} classified into a "
                f"milestone type")
    if state == STATE_DRIFT:
        return (f"the page renders {reading.get('profile_anchors')} profile anchor(s) but "
                f"_CATCHUP_CARD_LOCATORS matched NO cards — the #964 shape; re-ground from "
                f"`candidate_counts`")
    return ("the catch-up feed rendered nothing to read — an empty day or a page that did not "
            "load; grounds nothing")


def probe_catchup_cards(driver, sleep=time.sleep) -> dict:
    """#964/#1013: run the SHIPPED `_CATCHUP_CARD_LOCATORS` chain against the real catch-up feed and
    report which locator wins, how many cards each candidate matches, how many classify into a
    milestone, and the page's own profile-anchor count. Read-only — no card is clicked, no
    composer opened, nothing is sent."""
    # The production URL, not a copy of it — a probe that reads a different screen than the lane
    # does grounds nothing about the lane (the same reason the card walk is imported, not inlined).
    from cqc_lem.app.run_automation import (CATCHUP_URL, _CATCHUP_CARD_LOCATORS,
                                            _classify_catchup_moment)
    from cqc_lem.utilities.selenium_util import find_all_first

    driver.get(CATCHUP_URL)
    sleep(6)
    for _ in range(2):  # the feed lazy-loads, exactly as the production scan does
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        except Exception:
            break
        sleep(2)

    counts, winner = {}, None
    for by, selector in _CATCHUP_CARD_LOCATORS:
        try:
            found = len(driver.find_elements(by, selector))
        except Exception as e:
            found = f"<{type(e).__name__}>"
        counts[f"{by}={selector}"] = found
        if winner is None and isinstance(found, int) and found:
            winner = f"{by}={selector}"

    cards = find_all_first(driver, _CATCHUP_CARD_LOCATORS)
    texts, classified = [], 0
    for card in cards[:20]:
        try:
            text = " ".join((card.text or "").split())
        except Exception:
            continue
        if _classify_catchup_moment(text):
            classified += 1
        if len(texts) < 5:
            texts.append(text[:200])

    try:
        anchors = len(driver.find_elements(By.CSS_SELECTOR, "main a[href*='/in/']"))
    except Exception:
        anchors = 0
    reading = {"url": getattr(driver, "current_url", CATCHUP_URL),
               "cards_matched": len(cards), "winning_locator": winner,
               "candidate_counts": counts, "classified": classified,
               "profile_anchors": anchors, "sample_cards": texts,
               "page_text": page_text_sample(driver)}
    return graded(reading, catchup_state(reading), catchup_verdict(reading))


# ────────────────────────────────── group share box (#932) ───────────────────────────────────
def group_composer_state(reading: Optional[dict]) -> str:
    """Three-state grade for one group-composer read. A share box that opened an editor with a Post
    button is ok. A share box that resolved but produced neither is drift. NO share box at all is
    unknown, deliberately: an announcement / admin-only group legitimately renders none, and
    production already treats that as 'rotate past this group', not a failure."""
    reading = dict(reading or {})
    if not str(reading.get("page_text") or "").strip():
        return STATE_UNKNOWN
    if not reading.get("share_box_present"):
        return STATE_UNKNOWN
    return STATE_OK if (reading.get("editor_present") and reading.get("post_button_present")) \
        else STATE_DRIFT


def group_composer_verdict(reading: Optional[dict]) -> str:
    reading = dict(reading or {})
    state = group_composer_state(reading)
    if state == STATE_OK:
        return "share box, editor and Post button all resolved — a draft would publish here"
    if not str(reading.get("page_text") or "").strip():
        return "the group page did not render — re-run"
    if not reading.get("share_box_present"):
        return ("no share box on this group — production records it unpostable and rotates past "
                "it, which is correct for an announcement/admin-only group. Probe a group you can "
                "post in to ground the chain")
    return (f"the share box opened but "
            f"{'the editor' if not reading.get('editor_present') else 'the Post button'} did not "
            f"resolve — re-ground from `dialog_controls`")


def probe_group_composer(driver, group_id: str, sleep=time.sleep) -> dict:
    """#932/#1013: open a group's share box and report whether the editor and Post button resolve.

    Read-only in the way that matters: it OPENS the composer (like `--probe-composer` does on the
    feed) because the editor and Post button do not exist until it is open, then closes it with
    Escape. **Nothing is typed and the Post button is never clicked**, so no group post can result
    from running this."""
    from cqc_lem.app.run_automation import (_GROUP_EDITOR_LOCATORS, _GROUP_POST_BUTTON_LOCATORS,
                                            _GROUP_SHARE_BOX_LOCATORS)
    from cqc_lem.utilities.selenium_util import click_first, find_first
    from selenium.webdriver import ActionChains
    from selenium.webdriver.support.ui import WebDriverWait

    wait = WebDriverWait(driver, 10)
    url = f"https://www.linkedin.com/groups/{group_id}/"
    driver.get(url)
    sleep(6)
    reading = {"group_id": str(group_id), "url": getattr(driver, "current_url", url),
               "page_text": page_text_sample(driver)}

    share_box = click_first(driver, wait, _GROUP_SHARE_BOX_LOCATORS, "Group share box",
                            required=False, warn_on_miss=False, max_try=1)
    reading["share_box_present"] = share_box is not None
    if share_box is None:
        reading["visible_controls"] = visible_button_labels(driver)
        return graded(reading, group_composer_state(reading), group_composer_verdict(reading))

    sleep(3)
    editor = find_first(driver, wait, _GROUP_EDITOR_LOCATORS, "Group post editor",
                        required=False, warn_on_miss=False, max_try=1, visible_only=True)
    post_button = find_first(driver, wait, _GROUP_POST_BUTTON_LOCATORS, "Group Post button",
                             required=False, warn_on_miss=False, max_try=1, visible_only=True)
    reading.update({"editor_present": editor is not None,
                    "editor": element_evidence(editor) if editor is not None else None,
                    "post_button_present": post_button is not None,
                    "post_button": element_evidence(post_button) if post_button is not None
                    else None,
                    "dialog_controls": visible_button_labels(driver)})
    try:
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    except Exception:
        # Courtesy only — the driver is quit right after, and a failed Escape must not lose the
        # anchors this probe exists to report.
        pass
    return graded(reading, group_composer_state(reading), group_composer_verdict(reading))


# ─────────────────────────── company-page invite modal (#732) ────────────────────────────────
_CREDITS_TEXT_RE = re.compile(r"credits? available", re.IGNORECASE)


def company_invite_state(reading: Optional[dict]) -> str:
    """Three-state grade for one company-invite read. The page's own 'credits available' copy is
    the cross-check: credit text the parser could not read, or an invitee list the row locator
    missed on a page that renders one, is drift. A page that never rendered is unknown, and so is
    a genuinely exhausted credit pool — production stands down for that by design."""
    reading = dict(reading or {})
    if not str(reading.get("page_text") or "").strip():
        return STATE_UNKNOWN
    if not reading.get("credits_text_on_page"):
        return STATE_UNKNOWN
    if reading.get("total_credits") in (None, 0):
        return STATE_DRIFT
    if reading.get("credits_remaining") and not reading.get("invitee_rows"):
        return STATE_DRIFT
    return STATE_OK


def company_invite_verdict(reading: Optional[dict]) -> str:
    reading = dict(reading or {})
    state = company_invite_state(reading)
    if state == STATE_OK:
        return (f"credits {reading.get('credits_remaining')}/{reading.get('total_credits')} read, "
                f"{reading.get('invitee_rows')} invitee row(s) and "
                f"{reading.get('checkboxes')} checkbox(es) resolved")
    if not str(reading.get("page_text") or "").strip():
        return "the invite page did not render — re-run"
    if not reading.get("credits_text_on_page"):
        return ("no 'credits available' copy on the page — either this page has no invite "
                "affordance for this account, or the invite panel never opened; grounds nothing")
    if reading.get("total_credits") in (None, 0):
        return ("the page says 'credits available' but get_available_credits parsed nothing — "
                "re-ground its '<span>N/M</span>' locator from `page_text`")
    return (f"credits remain ({reading.get('credits_remaining')}) but the invitee-row locator "
            f"matched none — re-ground select_connection_checkboxes' list/checkbox XPaths")


def probe_company_invite(driver, user_id: int, company_url: str = "", sleep=time.sleep) -> dict:
    """#732/#1013: open the company page's invite panel and report what the invite lane's own
    readers see — the credit counter, the invitee rows, the checkboxes and the modal's Invite
    button. Read-only: **no checkbox is ticked and the Invite button is never clicked**, so no
    invitation can be sent by running this."""
    from cqc_lem.utilities.db import get_company_linked_in_url_for_user
    from cqc_lem.utilities.linkedin.company_page_inviter import get_available_credits
    from selenium.webdriver.support.ui import WebDriverWait

    wait = WebDriverWait(driver, 10)
    page = (company_url or get_company_linked_in_url_for_user(user_id) or "").rstrip("/")
    reading = {"user_id": user_id, "company_url": page}
    if not page:
        return graded(reading, STATE_UNKNOWN,
                      "no company page is configured for this user — the lane never opens a "
                      "browser, so there is nothing to ground")

    invite_url = f"{page}?invite=true"
    reading["invite_url"] = invite_url
    driver.get(invite_url)
    sleep(6)
    current, total = get_available_credits(driver, wait)
    text = page_text_sample(driver, limit=1200)

    def _count(selector: str) -> int:
        try:
            return len(driver.find_elements(By.XPATH, selector))
        except Exception:
            return 0

    reading.update({
        "url": getattr(driver, "current_url", invite_url),
        "page_text": text,
        "credits_text_on_page": bool(_CREDITS_TEXT_RE.search(text or "")),
        "credits_remaining": current,
        "total_credits": total,
        # The SAME XPaths select_connection_checkboxes drives, counted rather than clicked.
        "invitee_rows": _count("//div[contains(@class,'scaffold-finite-scroll__content')]//li"),
        "checkboxes": _count("//input[@type='checkbox' and contains(@id, 'invitee')]"),
        "invite_button_present": bool(_count(
            "//div[contains(@class,'modal')]//button[contains(@class,'artdeco-button--primary')]")),
        "visible_controls": visible_button_labels(driver)})
    return graded(reading, company_invite_state(reading), company_invite_verdict(reading))


def roster_connect_verdict(reading: dict) -> str:
    """What one activity-page reading proves about the #979 connect rung.

    'unknown' is the finding that matters here too, but it fails SAFE in the opposite direction from
    the follow lane: production treats it as "change nothing", so a rotated Pending/Message label
    does not send a duplicate invite — it just leaves the badge saying "connect with this account"
    after the user already has."""
    reading = dict(reading or {})
    state = reading.get("state")
    if state == "requested":
        return ("a Pending control resolved — production would advance this target to 'requested' "
                "for free, and never invite them again")
    if state == "connected":
        return ("a 1st-degree marker (or a Message control naming the owner — full name anywhere on "
                "the page, first name only inside their own card — with no Connect offered) "
                "resolved — production would finish the ladder at 'connected'")
    if not reading.get("owner_name"):
        return ("page owner's name unreadable from the <title>, so there is nothing to anchor the "
                "label match on — production returns unknown and changes nothing")
    return ("no owner-named Pending/Message control and no 1st-degree marker resolved — production "
            "returns unknown and leaves the stored state exactly as it was")


def probe_roster_connect(driver, profile_url: str,
                         sleep: Callable[[float], None] = time.sleep) -> dict:
    """#979: open a roster target's recent-activity page and report what the top card says about our
    CONNECTION to them — Pending, 1st-degree/Message, or nothing readable.

    STRICTLY read-only, and unlike the follow rung there is no clicker here at all: the invite goes
    out through the existing `invite_to_connect_now` rail on the profile page. This grounds the
    READING that decides whether a target is still waiting for one."""
    from cqc_lem.app.run_automation import (_activity_page_owner_name, _resolve_connect_state,
                                            _roster_activity_url)

    url = _roster_activity_url(profile_url)
    driver.get(url)
    sleep(5)
    state = _resolve_connect_state(driver, profile_url)
    reading = {"profile_url": profile_url,
               "activity_url": url,
               "url": getattr(driver, "current_url", url),
               "state": str(state),
               "owner_name": _activity_page_owner_name(driver),
               # The live labels the next locator pass would be written from — a bare "not found"
               # is not re-groundable.
               "visible_controls": visible_button_labels(driver)}
    reading["verdict"] = roster_connect_verdict(reading)
    return reading


def profile_experiences_verdict(reading: dict) -> str:
    """What one /details/experience/ reading proves about the #970 rebuild.

    The number that matters is `entities_with_dates`: an experience row is identified by its date
    range, so entities rendered but none dated means the page did not load (or the entity selector
    is gone), while dated entities that parse to nothing means the line grammar has moved."""
    reading = dict(reading or {})
    parsed = reading.get("experiences") or []
    dated = reading.get("entities_with_dates") or 0
    if not reading.get("entity_count"):
        return ("no entity nodes resolved at all — the page did not render (auth-wall / rate limit) "
                "or every selector in the ladder is gone; production returns []")
    if not dated:
        return (f"{reading['entity_count']} entity nodes, none carrying a date range — either this "
                "profile lists no experience, or the entities that hold it were not reached; "
                "`selector_counts` says which rungs matched at all")
    if not parsed:
        return (f"{dated} dated entities and ZERO parsed — selector rot: production would return [] "
                "and warn. The `entities` sample below is what the next parser pass rewrites from")
    positions = sum(len(e.get("positions") or []) for e in parsed)
    return (f"{len(parsed)} experiences / {positions} positions parsed from {dated} dated entities "
            "— compare company/title/dates against the profile as rendered before trusting it")


def probe_profile_experiences(driver, profile_url: str, max_entities: int = 6,
                              sleep: Callable[[float], None] = time.sleep) -> dict:
    """#970: open a profile's /details/experience/ page and report what the REBUILT parser reads,
    beside the raw evidence it read it from.

    Strictly read-only — it navigates and parses. `legacy_start_identifier` is the number the OLD
    positional parser branched on (`start_identifier_map`: 20/16/7/22 meant company/title/description
    /dates); anything else meant that entity was silently dropped or misread, which is what this
    issue is about. Reporting it is how a live run PROVES the old parser was dead rather than
    assuming it."""
    from bs4 import BeautifulSoup

    from cqc_lem.utilities.linkedin.scrapper import (_DATE_RANGE_RE, _EXPERIENCE_ENTITY_SELECTORS,
                                                     experience_entity_nodes,
                                                     get_start_identifier, parse_experience_entity,
                                                     parse_profile_experiences, source_as_row,
                                                     visible_lines)

    url = profile_url.rstrip("/") + "/details/experience/"
    driver.get(url)
    sleep(5)
    source = BeautifulSoup(driver.page_source, "html.parser")
    nodes, selector = experience_entity_nodes(source)
    # The 2026-08-03 run had to be followed by a hand-written hit-count pass to explain WHY the
    # wrong rung won (`data-view-name` absent, footer listitems matching). One probe run should
    # answer that, so the counts ship with the reading.
    counts = {css: len(source.select(css)) for css in _EXPERIENCE_ENTITY_SELECTORS}
    counts.update({css: len(source.select(css)) for css in
                   ("main", "[data-view-name]", "div[data-sdui-screen]", "div[role='list']",
                    "main div[role='list']")})

    entities = []
    dated = 0
    for node in nodes:
        lines = visible_lines(node)
        has_dates = any(_DATE_RANGE_RE.match(line) for line in lines)
        dated += 1 if has_dates else 0
        if len(entities) < max_entities:
            entities.append({"lines": lines,
                             "has_date_range": has_dates,
                             "legacy_start_identifier": get_start_identifier(source_as_row(node)),
                             "parsed": parse_experience_entity(lines) is not None})

    reading = {"profile_url": profile_url,
               "experience_url": url,
               "url": getattr(driver, "current_url", url),
               "entity_selector": selector,
               "entity_count": len(nodes),
               "entities_with_dates": dated,
               "selector_counts": counts,
               "entities": entities,
               "experiences": parse_profile_experiences(source)}
    reading["verdict"] = profile_experiences_verdict(reading)
    return reading


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
    return graded(reading, message_thread_state(reading), message_thread_verdict(reading))


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


# ──────────────────────────── the weekly drift sweep (issue #1013) ───────────────────────────
def sweep_summary(probes: Optional[dict]) -> dict:
    """Fold a sweep's per-probe states into the shape the issue filer plans from.

    `drift` is the only list that becomes GitHub issues. `unknown` is reported and NOT filed — a
    surface whose page never rendered grounds nothing, and filing it would bury the real drift
    under a weekly re-run of the same non-finding."""
    by_state = {STATE_OK: [], STATE_DRIFT: [], STATE_UNKNOWN: []}
    errors = []
    for key, reading in sorted((probes or {}).items()):
        state = (reading or {}).get("state")
        by_state.setdefault(state if state in by_state else STATE_UNKNOWN, []).append(key)
        if (reading or {}).get("probe_error"):
            errors.append(key)
    return {"probed": len(probes or {}), "ok": by_state[STATE_OK], "drift": by_state[STATE_DRIFT],
            "unknown": by_state[STATE_UNKNOWN], "errors": errors,
            "state": worst_state([(r or {}).get("state") for r in (probes or {}).values()])}


# LinkedIn's guest / challenge screens, by URL and by their own words. A session that has fallen
# out of login renders one of these at EVERY surface, and it renders prose and controls — so a
# per-probe grade reads "the page rendered but our locators found nothing", i.e. `drift`, on nearly
# every surface at once. That is a whole Monday of `priority:high` issues about a cookie.
_SIGNED_OUT_URL_PATHS = ("/authwall", "/checkpoint/", "/uas/login", "/login", "/challenge/",
                         "/captcha/", "/security-verification")


def sweep_session_state(driver, sleep=time.sleep) -> str:
    """`signed_in` / `signed_out` for the session the sweep is about to run on.

    Fails OPEN — only a POSITIVE signed-out signal (a challenge/guest URL, or LinkedIn's own guest
    copy) stands the sweep down. An unreadable page is exactly what `unknown` is for, and the
    per-probe grades handle it; refusing to sweep on an unreadable read would be the same silence
    this issue exists to end, just one level up."""
    try:
        driver.get(FEED_URL)
        sleep(4)
    except Exception:
        return "signed_in"
    url = str(getattr(driver, "current_url", "") or "")
    if any(path in url for path in _SIGNED_OUT_URL_PATHS):
        return "signed_out"
    try:
        from cqc_lem.utilities.linkedin.scrapper import _is_linkedin_error_page
    except Exception:
        return "signed_in"
    return "signed_out" if _is_linkedin_error_page(page_text_sample(driver, limit=1200)) \
        else "signed_in"


def run_sweep(driver, user_id: int, runners: Optional[dict] = None,
              keys: Optional[list] = None, profile_url: str = "",
              session_state: Optional[str] = None) -> dict:
    """Run every sweepable probe in ONE Chrome session and grade the lot.

    A probe that RAISES is recorded as `unknown` with its exception rather than killing the sweep:
    one rotated surface must not cost the reading of the nine that did not rotate — that is how a
    sweep silently covers less than it claims.

    A sweep on a signed-out session probes nothing: every surface would grade `drift` off the SAME
    auth wall, and the filer would open an issue per surface for one expired cookie. `unknown` is
    the honest grade for a page that never rendered, and `unknown` files nothing."""
    runners = runners if runners is not None else {
        "feed_sort": lambda: probe_feed_sort(driver),
        "feed_reactions": lambda: probe_feed_reactions(driver),
        "composer": lambda: probe_composer(driver),
        "profile_views": lambda: probe_profile_viewers(driver),
        "profile_scrape": lambda: probe_profile_scrape(
            driver, profile_url or _sweep_own_profile(driver, user_id)),
        "catchup_cards": lambda: probe_catchup_cards(driver),
        "company_invite": lambda: probe_company_invite(driver, user_id),
        "sent_invites": lambda: probe_sent_invites(driver),
        "appreciation_sources": lambda: probe_appreciation_sources(driver, user_id),
        "article_editor": lambda: probe_article_editor(driver),
    }
    wanted = [k for k in (keys or SWEEP_ORDER) if k in runners]
    session = session_state or sweep_session_state(driver)
    probes = {}
    for key in wanted:
        if session == "signed_out":
            probes[key] = {"state": STATE_UNKNOWN, "session": session,
                           "verdict": "the session is signed out (auth wall / challenge), so this "
                                      "surface never rendered — nothing is graded or filed; "
                                      "re-authenticate and re-run"}
            continue
        try:
            probes[key] = runners[key]() or {}
        except Exception as e:
            probes[key] = {"state": STATE_UNKNOWN, "probe_error": f"{type(e).__name__}: {e}"[:300],
                           "verdict": f"the probe itself raised ({type(e).__name__}) — grounds "
                                      f"nothing; re-run it"}
    # Surfaces the sweep deliberately cannot cover: named, never silently dropped. A coverage
    # matrix that reads as complete while a third of it never ran is the failure this issue is about.
    skipped = [s["key"] for s in SURFACES if not s["sweep"]]
    # The matrix rides along so the issue filer needs nothing from this module (it runs on the cron
    # host, which has neither selenium nor the app env).
    surfaces = {s["key"]: dict({k: s[k] for k in ("surface", "code", "flag")},
                               arg=s.get("arg", ""))
                for s in SURFACES if s["key"] in probes}
    return {"user_id": user_id, "session": session, "probes": probes, "skipped": skipped,
            "surfaces": surfaces, "summary": sweep_summary(probes)}


def _sweep_own_profile(driver, user_id: int) -> str:
    """The user's own profile URL — the one profile a sweep may scrape without a human choosing a
    target. It carries a degree badge of its own ('You'/no badge), so the header/name half is what
    it grounds; a stranger's profile is what `--profile-scrape <url>` is for."""
    from cqc_lem.app.run_automation import _own_profile_url
    return _own_profile_url(driver, user_id) or ""


def build_parser() -> "argparse.ArgumentParser":
    """The CLI, built apart from `main` so the coverage matrix can be checked against it without
    running a browser: a `SURFACES` row naming a flag that does not exist is a surface nobody can
    actually probe (issue #1013)."""
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
    parser.add_argument("--roster-follow", metavar="PROFILE_URL",
                        help="a roster target's profile URL — reports whether the top-card "
                             "Follow/Following control resolves on their activity page (#962). "
                             "Read-only: nothing is clicked, nobody is followed.")
    parser.add_argument("--roster-connect", metavar="PROFILE_URL",
                        help="a roster target's profile URL — reports what their activity page's "
                             "top card says about our connection to them: Pending, 1st-degree / "
                             "Message, or nothing readable (#979). Read-only: no invite is sent.")
    parser.add_argument("--appreciation-sources", action="store_true",
                        help="read the Recommendations Received section and the mentions "
                             "notification feed and report what the appreciation-DM triggers would "
                             "make of them (#968). Read-only: nothing is messaged and no ledger row "
                             "is claimed.")
    parser.add_argument("--sent-invites", action="store_true",
                        help="open Manage invitations -> Sent and report which pending rows resolve "
                             "and how their 'Sent ... ago' stamps parse (#969). Read-only: nothing "
                             "is withdrawn.")
    parser.add_argument("--sent-invite-days", type=int, default=None,
                        help="threshold to grade --sent-invites against (defaults to "
                             "STALE_INVITE_AGE_DAYS)")
    parser.add_argument("--permalink-comment", metavar="POST_URL",
                        help="a post permalink — reports whether the comment path's card -> Comment "
                             "action -> composer chain resolves there (#966). Opens the composer and "
                             "Escapes it; nothing is typed and no comment is left.")
    parser.add_argument("--profile-experiences", metavar="PROFILE_URL",
                        help="any profile URL — reports what the rebuilt experience parser reads "
                             "off /details/experience/, beside the raw lines it read (#970). "
                             "Read-only: it navigates and parses, nothing else.")
    parser.add_argument("--feed-sort", action="store_true",
                        help="report whether the home feed's 'Sort by -> Recent' control resolves "
                             "and whether the flip sticks (#817)")
    parser.add_argument("--profile-views", action="store_true",
                        help="report the profile-views analytics page's viewer-row locator state: "
                             "rows matched vs the page's own headline count, caption forms, and "
                             "whether the lazy-load scroll grows the list")
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
    parser.add_argument("--connect-dialog", metavar="PROFILE_URL",
                        help="navigate the custom-invite URL for this profile and report whether "
                             "the Connect dialog renders, plus every 'Invite … to connect' control "
                             "on the profile page that names someone ELSE (#1012). Read-only: no "
                             "Send, no Invite control and no menu is ever clicked.")
    parser.add_argument("--profile-scrape", metavar="PROFILE_URL",
                        help="run the shipped profile-header parser and degree-badge locators "
                             "against a real profile and cross-check both against the page's own "
                             "text (#1013). Read-only.")
    parser.add_argument("--catchup-cards", action="store_true",
                        help="run the shipped _CATCHUP_CARD_LOCATORS chain against the catch-up "
                             "feed and grade it against the page's own profile-anchor count "
                             "(#964). Read-only: no card is clicked, nothing is sent.")
    parser.add_argument("--group-composer", metavar="GROUP_ID",
                        help="open this group's share box and report whether the editor and Post "
                             "button resolve (#932). Types nothing and NEVER clicks Post.")
    parser.add_argument("--company-invite", action="store_true",
                        help="open the company page's invite panel and report the credit counter, "
                             "invitee rows and checkboxes the invite lane reads (#732). Ticks no "
                             "checkbox and never clicks Invite.")
    parser.add_argument("--company-url", default="",
                        help="company page URL for --company-invite (defaults to the user's "
                             "configured page)")
    parser.add_argument("--sweep", action="store_true",
                        help="run every target-free probe in ONE session and grade each ok/drift/"
                             "unknown (#1013). This is what the weekly drift cron runs.")
    parser.add_argument("--sweep-profile-url", default="",
                        help="profile URL for the sweep's --profile-scrape leg; defaults to the "
                             "user's OWN profile, which carries no degree badge and therefore "
                             "leaves the degree half ungrounded")
    parser.add_argument("--surfaces", action="store_true",
                        help="print the Selenium surface / probe coverage matrix as JSON and exit "
                             "(no browser, no network)")
    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.surfaces:
        print(json.dumps({"surfaces": list(SURFACES), "sweep": list(SWEEP_ORDER)}, indent=2))
        return 0

    if not (args.post_url or args.probe_composer or args.comment_outcome_url or args.dm_thread_url
            or args.article_editor_url or args.feed_sort or args.reaction_probe
            or args.roster_follow or args.roster_connect or args.appreciation_sources
            or args.sent_invites or args.profile_views or args.connect_dialog
            or args.profile_scrape or args.profile_experiences or args.catchup_cards
            or args.group_composer or args.company_invite or args.permalink_comment
            or args.sweep):
        parser.error("nothing to probe — pass --sweep, --surfaces, --post-url, "
                     "--comment-outcome-url, --dm-thread-url, --article-editor-url, --feed-sort, "
                     "--profile-views, --profile-scrape, --profile-experiences, "
                     "--connect-dialog, --catchup-cards, "
                     "--group-composer, --company-invite, --reaction-probe, --roster-follow, "
                     "--roster-connect, --appreciation-sources, --sent-invites, "
                     "--permalink-comment and/or "
                     "--probe-composer")

    from cqc_lem.app.run_automation import get_current_profile
    from cqc_lem.utilities.selenium_util import quit_gracefully

    driver, _wait, _email, profile = get_current_profile(user_id=args.user_id,
                                                        session_name="Live Validation",
                                                        debug=args.watch)
    report = {"user_id": args.user_id}
    try:
        if args.sweep:
            # The sweep IS the report: it replaces the per-probe keys wholesale, so the weekly
            # cron's JSON has exactly one shape for the issue filer to plan from.
            report = run_sweep(driver, args.user_id, profile_url=args.sweep_profile_url)
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
        if args.profile_views:
            report["profile_views"] = probe_profile_viewers(driver)
        if args.roster_follow:
            report["roster_follow"] = probe_roster_follow(driver, args.roster_follow)
        if args.roster_connect:
            report["roster_connect"] = probe_roster_connect(driver, args.roster_connect)
        if args.appreciation_sources:
            report["appreciation_sources"] = probe_appreciation_sources(
                driver, args.user_id, str(getattr(profile, "profile_url", "") or ""))
        if args.sent_invites:
            report["sent_invites"] = probe_sent_invites(driver, args.sent_invite_days)
        if args.connect_dialog:
            report["connect_dialog"] = probe_connect_dialog(driver, args.connect_dialog)
        if args.profile_scrape:
            report["profile_scrape"] = probe_profile_scrape(driver, args.profile_scrape)
        if args.profile_experiences:
            report["profile_experiences"] = probe_profile_experiences(driver,
                                                                      args.profile_experiences)
        if args.catchup_cards:
            report["catchup_cards"] = probe_catchup_cards(driver)
        if args.group_composer:
            report["group_composer"] = probe_group_composer(driver, args.group_composer)
        if args.company_invite:
            report["company_invite"] = probe_company_invite(driver, args.user_id, args.company_url)
        if args.permalink_comment:
            report["permalink_comment"] = probe_permalink_comment(driver, args.permalink_comment)
        if args.probe_composer:
            report["composer"] = probe_composer(driver)
        if args.article_editor_url:
            report["article_editor"] = probe_article_editor(driver, args.article_editor_url)
        if args.reaction_probe:
            report["feed_reactions"] = probe_feed_reactions(
                driver, max_cards=args.reaction_cards, open_menu=args.reaction_open_menu)
    finally:
        quit_gracefully(driver)

    print(REPORT_JSON_BEGIN)
    print(json.dumps(report, indent=2))
    print(REPORT_JSON_END)
    return 0


if __name__ == "__main__":
    sys.exit(main())
