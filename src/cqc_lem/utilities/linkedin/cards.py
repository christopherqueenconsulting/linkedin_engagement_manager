"""Reading a LinkedIn feed card: its identity, its permalink, its counts (#1154).

Lifted VERBATIM out of `app/run_automation.py`, which had grown to 9,162 lines and was the only
home for the Selenium mechanics every engagement cluster shares. Nothing here is Celery, a task, or
application policy — it is the DOM/text layer that answers "which post is this, and what do its
numbers say", so it belongs beside the other `utilities/linkedin/*` mechanics rather than inside the
module that schedules work.

The invariants that travel with these functions:

* **Identity is the URN, never a content hash.** A "…see more" toggle or our own just-posted comment
  mutates a card's text, so hashing it mints a SECOND key for the SAME post — which is how feed
  commenting posted twice on one post (#474, recurred as #580). `_feed_post_urn_from_card` walks UP
  for the `urn:li:(activity|ugcPost|share)` attribute and STOPS before any ancestor that spans two
  cards, so it can never hand back a neighbour's URN.
* **A case-sensitive XPath match against a LinkedIn label silently never fires.** XPath 1.0 has no
  `lower-case()`, so `_x_lower` is the case fold every locator chain in the tree goes through.
* **Zero is not always zero.** `_post_social_counts` returns 0 on a miss, which reads identically to
  a post with no engagement — the zero-walk cross-check (#1021) is what tells them apart, and it
  lives with its caller.

The names keep their leading underscore: they moved verbatim, so a reader grepping either module
finds one spelling, and the test patches that follow them are a pure module-path change.
"""

import re

from selenium.webdriver.common.by import By

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
    that fails SILENTLY — a null card is indistinguishable from an empty feed.
    """
    return driver.execute_script(_CARD_FOR_TEXTBOX_JS, box)


# Canonical LinkedIn post identity. The activity/ugcPost/share URN is stable across re-renders;
# it's the only reliable dedup anchor. A content hash is NOT — a "…see more" toggle or our own
# just-posted comment mutates the card's text and yields a different hash for the SAME post,
# which is how feed commenting posted two comments on one post (issue #474).
_URN_RE = re.compile(r"urn:li:(?:activity|ugcPost|share):\d+", re.I)


def _normalize_post_text(content: str) -> str:
    """Collapse the volatile bits of a card's rendered text so the SAME post hashes the same
    across re-renders: drop the 'see more'/'…more' expander tokens and ellipses, collapse all
    whitespace, lowercase. Used only for the no-URN fallback key + the per-run fingerprint.
    """
    t = (content or "").lower()
    t = re.sub(r"\s*(?:…|\.\.\.)?\s*see\s+more\b", " ", t)
    t = re.sub(r"\s*(?:…|\.\.\.)\s*more\b", " ", t)
    t = t.replace("…", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _norm_prefix(content: str, limit: int) -> str:
    """Normalized post text cut to `limit` chars on a word boundary — the same prefix for the
    collapsed and the expanded render of one post.
    """
    norm = _normalize_post_text(content)
    if len(norm) <= limit:
        return norm
    head, sep, _tail = norm[:limit].rpartition(" ")
    return head if sep else norm[:limit]


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
    descendants, then falls back to a regex over the card's own HTML. Lowercased URN or None.
    """
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


def _post_permalink_from_card(card):
    """Real LinkedIn permalink for a feed post, read from its /feed/update/ anchor (the SDUI
    card has no data-urn). Returns a normalized https URL or None.
    """
    try:
        for a in card.find_elements(By.CSS_SELECTOR, "a[href*='/feed/update/']"):
            href = (a.get_attribute("href") or "").split("?")[0]
            if "/feed/update/" in href:
                return href.rstrip("/") + "/"
        return None
    except Exception:
        return None


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


def _stacked_counts(text: str) -> dict:
    """Counts for the post-analytics layout, where a label and its value are on adjacent lines.
    Only exact label lines pair up, and only with a neighbour that is a bare count — so a row's
    value can never be read as the next row's, and post body text is ignored.
    """
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
    engagement score (#387); reactions/comments feed the low-weight feed 'activity' scoring signal.
    """
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

# Declared because this module exists to be imported FROM: the `app.engagement.*` lanes read these
# selectors and helpers, and nothing in this file uses several of them, so CodeQL reports
# them as unused globals without it (py/unused-global-variable).
__all__ = [
    "_BARE_COUNT_RE",
    "_CARD_FOR_TEXTBOX_JS",
    "_COMMENTS_RE",
    "_COMMENT_ACTION_JS",
    "_COUNT",
    "_COUNT_MULT",
    "_FEED_POST_TEXT_SEL",
    "_IMPRESSIONS_RE",
    "_REACTIONS_RE",
    "_REPOSTS_RE",
    "_SAVES_RE",
    "_SEP",
    "_STACKED_LABEL_FIRST",
    "_STACKED_VALUE_FIRST",
    "_URN_RE",
    "_URN_SCAN_JS",
    "_X_AZ_LOWER",
    "_X_AZ_UPPER",
    "_X_LOWER_ARIA",
    "_X_LOWER_TEXT",
    "_card_for_textbox",
    "_feed_post_urn_from_card",
    "_norm_prefix",
    "_normalize_post_text",
    "_parse_count",
    "_post_permalink_from_card",
    "_post_social_counts",
    "_stacked_counts",
    "_x_lower",
]
