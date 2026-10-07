"""The post image's RHYTHM — which treatment, panel, grade and shot the next post gets (#2241).

The owner's anti-monotony round: every post image was the same composite — a photograph beside a
charcoal type panel — so a profile grid read as one image posted N times. This module decides, per
post, deterministically from the author's own post receipts (``post_image.recent_post_receipts``):

- **Treatment** — ``photo_only`` (the AI scene, no text), ``typeset_card`` (the composite), or a
  code-drawn card: ``data_card`` (the #2254 ``stat_card`` renderer: a verified numeral, its claim,
  a source line) or ``quote_card`` (one sentence of the post, verbatim, over the author's byline).
  ``card_share`` (brand kit, default 0.4) is the share of posts that get a ``typeset_card``,
  honoured over the last ``CARD_SHARE_WINDOW`` posts; the rest rotate least-recently-used. A
  treatment the post cannot satisfy is skipped with its reason; one that fails at render time
  falls to the next.
- **Panel variant** — a ``typeset_card`` rotates charcoal / off-white / gold
  (``image_compose.PANEL_VARIANTS``), least-recently-used among the variants whose headline
  clears 4.5:1 in the author's brand.
- **Photo grade** — daylight / cool interior / warm dusk (``image_brief.PHOTO_GRADES``) on every
  AI-rendered scene.
- **The sameness gate** — over the last ``SAMENESS_WINDOW`` receipts, no dimension (treatment,
  layout, panel, shot, grade) may run more than ``MAX_RUN`` posts in a row; a pick that would is
  re-rolled to the next least-recently-used option. The gate outranks ``card_share``: a share of
  1.0 still yields at most two typeset cards in a row.
- **Visual style** (showcase round 4: two claymation renders ran back to back) — every image has
  ONE ``style``: an editorial art style (claymation, cut collage, risograph, editorial photo), a
  people photo, a code-drawn card or a quote card. The same style may not appear in two
  consecutive posts, and quote cards are capped at ``QUOTE_CAP`` per ``QUOTE_CAP_WINDOW`` posts.
  The last-resort card is the one exception: it is the guarantee that a post never ships bare.

Newsletter covers never come through here — they keep their one consistent card.

Pure except ``pick_quote``, the one ``lem-simple`` tiebreak among at most five deterministic
quote candidates. ``docs/image-stack.md``.
"""

import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional

from cqc_lem.utilities.logger import log_debug
from cqc_lem.utilities.observability import llm_step

TREATMENT_PHOTO_ONLY = "photo_only"
TREATMENT_TYPESET_CARD = "typeset_card"
TREATMENT_DATA_CARD = "data_card"
TREATMENT_QUOTE_CARD = "quote_card"
POST_TREATMENTS = (TREATMENT_TYPESET_CARD, TREATMENT_PHOTO_ONLY, TREATMENT_DATA_CARD,
                   TREATMENT_QUOTE_CARD)
# The treatments that are NOT the typeset card, in their tie order.
_OTHER_TREATMENTS = (TREATMENT_PHOTO_ONLY, TREATMENT_DATA_CARD, TREATMENT_QUOTE_CARD)
# Treatments whose image is a rendered AI scene (and so take a photo grade).
AI_SCENE_TREATMENTS = frozenset({TREATMENT_PHOTO_ONLY, TREATMENT_TYPESET_CARD})

CARD_SHARE_WINDOW = 10
SAMENESS_WINDOW = 5
MAX_RUN = 2
# The receipt dimensions the sameness gate reads, in the order they are recorded.
RHYTHM_DIMENSIONS = ("treatment", "layout", "panel", "shot", "grade", "setting", "style",
                     "card_layout")

# Showcase round 4: the image's visual STYLE is a dimension of its own. An editorial concept's
# style is its ``image_concept.ART_STYLES`` key; the rest are named here.
STYLE_PEOPLE_PHOTO = "people_photo"
STYLE_CODE_DRAWN = "code_drawn_card"
STYLE_QUOTE_CARD = "quote_card"
QUOTE_CAP = 2
QUOTE_CAP_WINDOW = 6

# #2241 showcase B: two consecutive people posts were both "in a warehouse with boxes". A setting
# CLASS may not appear in two consecutive AI renders. Matched by keyword, earliest in the text
# first (a longer keyword wins a tie, so "home office" is never read as "office").
SETTING_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("warehouse", ("warehouse", "loading dock", "stockroom", "pallet", "boxes", "depot",
                   "distribution center", "storage room")),
    ("home_office", ("home office", "kitchen table", "living room", "spare room", "at home",
                     "apartment")),
    ("cafe", ("café", "cafe", "coffee shop", "coffeehouse")),
    ("conference", ("conference", "expo", "trade show", "summit", "auditorium", "keynote",
                    "event hall", "hallway")),
    ("shop_floor", ("shop floor", "factory", "workshop", "production line", "plant floor",
                    "assembly line", "manufacturing")),
    ("retail", ("store", "storefront", "boutique", "checkout counter", "shop counter")),
    ("outdoors", ("street", "park", "sidewalk", "outdoors", "outside")),
    ("office", ("office", "meeting room", "boardroom", "cubicle", "desk", "workspace",
                "co-working", "coworking")),
)
# Used only when the article itself names no alternative setting.
DEFAULT_SETTINGS = {"office": "a bright open-plan office", "home_office": "a home office by a window",
                    "cafe": "a quiet café table", "conference": "a conference hallway"}

QUOTE_MAX_CHARS = 160
QUOTE_MIN_CHARS = 30
QUOTE_MIN_WORDS = 5
MAX_QUOTE_CANDIDATES = 5


def lru_order(options: Sequence[str], recent: Sequence[str], seed: str = "") -> list[str]:
    """Every option, least-recently-used first.

    Options absent from ``recent`` come first (in ``options`` order), then the used ones from the
    one used longest ago to the most recent. With no history at all a stable hash of ``seed``
    rotates the start, so a new author's first posts still vary.

    Args:
        options: The choices, in tie order.
        recent: What recent posts used, most recent first.
        seed: Stable per-piece text for the no-history case.

    Returns:
        ``options`` reordered.
    """
    options = list(options)
    seen = [r for r in recent if r in options]
    if not options:
        return []
    if not seen:
        start = sum(map(ord, seed or "")) % len(options)
        return options[start:] + options[:start]
    unused = [o for o in options if o not in seen]
    used = sorted((o for o in options if o in seen), key=seen.index, reverse=True)
    return unused + used


def _setting_hits(text: Optional[str]) -> list[tuple[int, int, str, str]]:
    """``(start, -length, class, matched text)`` for every setting keyword in ``text``."""
    lowered = (text or "").lower()
    hits = []
    for name, keywords in SETTING_CLASSES:
        for keyword in keywords:
            for match in re.finditer(rf"\b{re.escape(keyword)}\b", lowered):
                hits.append((match.start(), -len(keyword), name,
                             (text or "")[match.start():match.end()]))
    return sorted(hits)


def setting_class(text: Optional[str]) -> str:
    """The setting CLASS a scene description is in — warehouse, office, café… — or ``""``.

    Args:
        text: A concept's setting, or a render prompt.

    Returns:
        The class of the earliest setting keyword, or ``""`` when none is named.
    """
    hits = _setting_hits(text)
    return hits[0][2] if hits else ""


def reroll_setting(setting: str, recent: Sequence[Any], article: str) -> tuple[str, bool]:
    """Keep Stage 1's setting unless its class repeats the last AI render's; then pick another.

    A setting the concept leaves EMPTY is re-rolled too when there is a recent class, because the
    brief author then picks freely — which is how the warehouse came back. The alternative comes
    from the ARTICLE's own words (the first setting keyword of a different class it names); only
    an article naming none takes a neutral default.

    Args:
        setting: Stage 1's setting ('' when it named none).
        recent: The setting classes of recent posts, most recent first; None for a non-AI post.
        article: The post text.

    Returns:
        ``(setting, rerolled)`` — the setting to brief with ('' = Stage 1's own, unchanged).
    """
    last = next((r for r in recent if r), "")
    if not last or (setting and setting_class(setting) != last):
        return "", False
    for _start, _length, name, words in _setting_hits(article):
        if name != last:
            return words, True
    for name, phrase in DEFAULT_SETTINGS.items():
        if name != last:
            return phrase, True
    return "", False


def repeats_run(recent: Sequence[Any], value: Any, max_run: int = MAX_RUN) -> bool:
    """Would ``value`` make the last ``max_run`` values a run of ``max_run + 1``?

    Args:
        recent: The dimension's recent values, most recent first.
        value: The candidate; an empty one never counts as a repeat.
        max_run: The longest run allowed.

    Returns:
        True when the newest ``max_run`` values all equal ``value``.
    """
    window = list(recent)[:max_run]
    return bool(value) and len(window) == max_run and all(v == value for v in window)


def sameness_pick(ordered: Sequence[str], recent: Sequence[Any],
                  max_run: int = MAX_RUN) -> tuple[Optional[str], bool]:
    """The first option in ``ordered`` that does not extend a run past ``max_run``.

    Args:
        ordered: The candidates, preferred first.
        recent: The dimension's recent values, most recent first.
        max_run: The longest run allowed.

    Returns:
        ``(pick, rerolled)`` — ``rerolled`` is True when the preferred option was passed over. With
        a single option there is nothing to re-roll to, so it is kept. ``(None, False)`` for none.
    """
    for index, option in enumerate(ordered):
        if not repeats_run(recent, option, max_run):
            return option, index > 0
    return (ordered[0] if ordered else None), False


def pick_dimension(options: Sequence[str], recent: Sequence[Any],
                   seed: str = "") -> tuple[Optional[str], bool]:
    """Least-recently-used pick for one dimension, through the sameness gate.

    Args:
        options: The dimension's values.
        recent: Its recent values, most recent first.
        seed: Stable per-piece text for the no-history case.

    Returns:
        ``(value, rerolled)``.
    """
    return sameness_pick(lru_order(options, [r for r in recent if r], seed), recent)


def gate_value(value: Optional[str], options: Sequence[str], recent: Sequence[Any],
               seed: str = "") -> tuple[Optional[str], bool]:
    """Keep a value already chosen upstream unless it would run too long; then re-roll it.

    Stage 1 picks the layout and shot from the same receipts, so this rarely fires — it exists
    for the case where the upstream pick ignored history (a concept built without it).

    Args:
        value: The upstream pick; '' or None passes through.
        options: The dimension's values.
        recent: Its recent values, most recent first.
        seed: Stable per-piece text.

    Returns:
        ``(value, rerolled)``.
    """
    if not value or not repeats_run(recent, value):
        return value, False
    others = [o for o in lru_order(options, [r for r in recent if r], seed) if o != value]
    pick, _ = sameness_pick(others, recent)
    return (pick or value), pick is not None


def wants_card(recent_treatments: Sequence[str], card_share: float,
               window: int = CARD_SHARE_WINDOW) -> bool:
    """Does the next post need a ``typeset_card`` to keep ``card_share`` over the window?

    Deficit rule over the last ``window`` posts including this one: a card is due when the
    rounded target for ``len(history) + 1`` posts exceeds the cards already shipped. With 0.4 and
    no history the sequence is no, card, no, card, no, no, card, no, card, no — four in ten.

    Args:
        recent_treatments: Treatments of recent posts, most recent first.
        card_share: The target share, 0-1.
        window: The rolling window.

    Returns:
        True when a card is due.
    """
    history = list(recent_treatments)[:max(0, window - 1)]
    cards = sum(1 for t in history if t == TREATMENT_TYPESET_CARD)
    share = min(1.0, max(0.0, float(card_share)))
    return math.floor(share * (len(history) + 1) + 0.5) - cards >= 1


def style_of(treatment: Optional[str], archetype: Optional[str] = None,
             art_style: Optional[str] = None, last_resort: bool = False) -> Optional[str]:
    """The ONE visual style an image shipped in, or None when it is not known.

    Args:
        treatment: The post treatment that shipped.
        archetype: The archetype rendered (``archetype_rendered``).
        art_style: The editorial concept's ``ART_STYLES`` key.
        last_resort: The image is the last-resort typeset card.

    Returns:
        ``quote_card``, ``code_drawn_card``, ``people_photo``, an art style key, or None.
    """
    from cqc_lem.utilities.ai.image_concept import (
        ARCHETYPE_EDITORIAL,
        ARCHETYPE_PEOPLE,
        CODE_DRAWN_ARCHETYPES,
    )

    if treatment == TREATMENT_QUOTE_CARD or archetype == STYLE_QUOTE_CARD:
        return STYLE_QUOTE_CARD
    if (last_resort or treatment == TREATMENT_DATA_CARD or archetype in CODE_DRAWN_ARCHETYPES
            or archetype == TREATMENT_TYPESET_CARD):
        return STYLE_CODE_DRAWN
    if archetype == ARCHETYPE_PEOPLE:
        return STYLE_PEOPLE_PHOTO
    if archetype == ARCHETYPE_EDITORIAL:
        return art_style or None
    return None


def style_blocks(recent_styles: Sequence[Any]) -> dict[str, str]:
    """``{treatment: reason}`` for the treatments the style rule rules out for the next post.

    A quote card may not follow a quote card, nor make a third in ``QUOTE_CAP_WINDOW`` posts; a
    data card (always code-drawn) may not follow a code-drawn card.

    Args:
        recent_styles: Styles of recent posts, most recent first (None = unknown).

    Returns:
        The blocked treatments with their reasons.
    """
    recent = list(recent_styles)
    last = recent[0] if recent else None
    blocked: dict[str, str] = {}
    quotes = sum(1 for v in recent[:QUOTE_CAP_WINDOW - 1] if v == STYLE_QUOTE_CARD)
    if last == STYLE_QUOTE_CARD:
        blocked[TREATMENT_QUOTE_CARD] = "style repeats: the last post was a quote card"
    elif quotes >= QUOTE_CAP:
        blocked[TREATMENT_QUOTE_CARD] = (f"quote cards are capped at {QUOTE_CAP} per "
                                         f"{QUOTE_CAP_WINDOW} posts")
    if last == STYLE_CODE_DRAWN:
        blocked[TREATMENT_DATA_CARD] = "style repeats: the last post was a code-drawn card"
    return blocked


def blocked_archetypes(last_style: Optional[str]) -> frozenset:
    """The archetypes a rendered post must avoid so its style differs from the last post's.

    Args:
        last_style: The most recent post's style.

    Returns:
        ``people_scene`` after a people photo, every code-drawn archetype after a code-drawn card.
    """
    from cqc_lem.utilities.ai.image_concept import ARCHETYPE_PEOPLE, CODE_DRAWN_ARCHETYPES

    if last_style == STYLE_PEOPLE_PHOTO:
        return frozenset({ARCHETYPE_PEOPLE})
    if last_style == STYLE_CODE_DRAWN:
        return frozenset(CODE_DRAWN_ARCHETYPES)
    return frozenset()


@dataclass(frozen=True)
class TreatmentPlan:
    """The treatments to try for one post, in order, and why any were skipped.

    Attributes:
        chain: Satisfiable treatments, first choice first; each later one is the fallback.
        skipped: ``(treatment, reason)`` for every treatment the post cannot satisfy.
        card_wanted: Whether ``card_share`` asked for a typeset card this post.
        rerolled: Whether the sameness gate passed over the first choice.
    """

    chain: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]
    card_wanted: bool
    rerolled: bool


def plan_treatments(recent_treatments: Sequence[str], card_share: float,
                    unavailable: Optional[Mapping[str, str]] = None,
                    seed: str = "") -> TreatmentPlan:
    """Order the post treatments for the next post.

    ``card_share`` decides whether the typeset card leads or comes last; the other treatments are
    ordered least-recently-used. Unsatisfiable treatments are dropped with their reason, and the
    sameness gate then moves the first choice behind the others if it would be a third in a row.

    Args:
        recent_treatments: Treatments of recent posts, most recent first.
        card_share: The brand kit's share of typeset cards.
        unavailable: ``{treatment: reason}`` for treatments this post cannot take.
        seed: Stable per-piece text for the no-history case.

    Returns:
        The plan.
    """
    unavailable = dict(unavailable or {})
    recent = [t for t in recent_treatments if t][:CARD_SHARE_WINDOW]
    card = wants_card(recent, card_share)
    others = lru_order(_OTHER_TREATMENTS, recent, seed)
    order = [TREATMENT_TYPESET_CARD] + others if card else others + [TREATMENT_TYPESET_CARD]
    skipped = tuple((t, unavailable[t]) for t in order if t in unavailable)
    chain = [t for t in order if t not in unavailable]
    head, rerolled = sameness_pick(chain, recent)
    if rerolled and head is not None:
        chain.remove(head)
        chain.insert(0, head)
    return TreatmentPlan(chain=tuple(chain), skipped=skipped, card_wanted=card,
                         rerolled=rerolled)


def next_treatment(remaining: Sequence[str], recent_treatments: Sequence[str]) -> Optional[str]:
    """The fallback after a render-time failure: the next remaining treatment, through the gate.

    Args:
        remaining: The plan's chain minus what was tried.
        recent_treatments: Treatments of recent posts, most recent first.

    Returns:
        The treatment to try next, or None when the chain is spent.
    """
    pick, _ = sameness_pick(list(remaining), list(recent_treatments))
    return pick


# --- Opinion posts and the verbatim quote ------------------------------------------------------

_STANCE = re.compile(
    r"\b(?:i think|i believe|in my (?:view|opinion|experience)|unpopular opinion|hot take|"
    r"here'?s the thing|the truth is|the problem is|the real (?:problem|cost|reason)|"
    r"stop \w+ing|you (?:should|shouldn't|don't need)|should(?:n't| not)?|nobody|"
    r"most (?:people|founders|teams|businesses|owners)|overrated|underrated|is a myth|"
    r"isn'?t the (?:problem|answer)|wrong)\b", re.IGNORECASE)


def opinion_signal(concept: Any, text: str) -> str:
    """Why this post reads as an OPINION (a quote card fits it), or '' when it does not.

    Stage 1's signals first: a ``contrast`` hook, or a negative/mixed valence on a thesis that
    asserts something. Then the post's own stance markers ("I think", "most founders", "stop
    …ing", "unpopular opinion"). A positive, unmarked report of results is not an opinion.

    Args:
        concept: Stage 1's ``ImageConcept`` (None means no signal).
        text: The post.

    Returns:
        A short reason, or ''.
    """
    from cqc_lem.utilities.ai.image_concept import asserts_something

    thesis = getattr(concept, "thesis", "") or ""
    if concept is None or not thesis:
        return ""
    if getattr(concept, "hook_shape", "") == "contrast":
        return "contrast hook"
    valence = getattr(concept, "valence", "") or ""
    if valence in ("negative", "mixed") and asserts_something(thesis):
        return f"{valence} valence on an asserting thesis"
    match = _STANCE.search((text or "").replace("’", "'"))
    return f'stance marker "{match.group(0).lower()}"' if match else ""


# A sentence: a run up to its terminal punctuation (and any closing quote), or a line.
# Terminal punctuation counts only before whitespace or the end, so "$3.5M" and "4.5:1" stay whole.
_SENTENCE_SPAN = re.compile(r"(?:[^.!?\n]|[.!?](?=[^\s.!?]))+[.!?]+[\"'”’)]*(?=\s|$)")
_LINK = re.compile(r"https?://|www\.|\.com\b|@\w", re.IGNORECASE)
# Punctuation the brand font sets; any other symbol (an emoji, an arrow) disqualifies.
_ALLOWED_SYMBOLS = frozenset("$€£%&'’‘\"“”-–—…,.;:!?()/+")


def _settable(sentence: str) -> bool:
    for ch in sentence:
        if ch.isalnum() or ch.isspace() or ch in _ALLOWED_SYMBOLS:
            continue
        return False
    return not any(unicodedata.category(ch) == "So" for ch in sentence)


def is_verbatim(sentence: str, text: str) -> bool:
    """The deterministic quote check: is ``sentence`` an exact substring of the post?

    Args:
        sentence: The candidate quote.
        text: The post.

    Returns:
        True only for a non-empty exact substring — no normalising, no paraphrase.
    """
    return bool(sentence) and sentence in (text or "")


def quote_candidates(text: str, thesis: str = "",
                     limit: int = MAX_QUOTE_CANDIDATES) -> list[str]:
    """The post's most quotable sentences, deterministically, best first.

    A candidate is a complete sentence ending in ``.`` or ``!`` (never a question), 30-160
    characters and at least five words, opening on a letter or a quote mark, with no hashtag, link
    or mention and nothing the brand font cannot set. Each is the post's own exact substring.
    Ranked by stance markers (3 each), overlap with the thesis (at most 3), first person and a
    50-140 character length, then by position.

    Args:
        text: The post.
        thesis: Stage 1's thesis, for the overlap score.
        limit: The most to return.

    Returns:
        Up to ``limit`` sentences.
    """
    from cqc_lem.utilities.ai.image_concept import _content_tokens

    thesis_tokens = set(_content_tokens(thesis or ""))
    scored: list[tuple[int, int, str]] = []
    for position, match in enumerate(_SENTENCE_SPAN.finditer(text or "")):
        sentence = match.group(0).strip()
        bare = sentence.rstrip("\"'”’)")
        if not (QUOTE_MIN_CHARS <= len(sentence) <= QUOTE_MAX_CHARS):
            continue
        if not bare.endswith((".", "!")) or bare.endswith(".."):
            continue
        if len(sentence.split()) < QUOTE_MIN_WORDS or not (sentence[0].isalpha()
                                                         or sentence[0] in "\"“'‘"):
            continue
        if "#" in sentence or _LINK.search(sentence) or not _settable(sentence):
            continue
        if not is_verbatim(sentence, text):
            continue
        score = 3 * len(_STANCE.findall(sentence.replace("’", "'")))
        score += min(3, len(thesis_tokens & set(_content_tokens(sentence))))
        score += 1 if re.search(r"\b(?:I|I'm|I've|my|we|our)\b", sentence) else 0
        score += 1 if 50 <= len(sentence) <= 140 else 0
        scored.append((-score, position, sentence))
    seen: list[str] = []
    for _neg, _pos, sentence in sorted(scored):
        if sentence not in seen:
            seen.append(sentence)
    return seen[:max(0, limit)]


_QUOTE_PICK_PROMPT = """These sentences are from ONE LinkedIn post by its author. Pick the ONE \
that would make the strongest pull-quote on its own: a complete, self-contained claim that \
reads without context and carries the post's point.

The post's thesis: {thesis}

{numbered}

Respond with ONLY a JSON object: {{"index": <the sentence's number>}}"""


@llm_step("post_quote_pick")
def pick_quote(candidates: Sequence[str], thesis: str = "",
               user_id: Optional[int] = None) -> tuple[str, str]:
    """The pull-quote: ONE ``lem-simple`` tiebreak among the deterministic candidates.

    One candidate needs no call. Any failure — an outage, an unparsable reply, an index out of
    range — keeps the deterministic best (the first candidate); the quote is never authored.

    Args:
        candidates: ``quote_candidates`` output, best first.
        thesis: Stage 1's thesis, for context.
        user_id: The author, for log context.

    Returns:
        ``(sentence, how)`` — ``how`` is ``only``, ``llm`` or ``deterministic``; ``('', 'none')``
        with no candidates.
    """
    candidates = list(candidates)[:MAX_QUOTE_CANDIDATES]
    if not candidates:
        return "", "none"
    if len(candidates) == 1:
        return candidates[0], "only"
    from cqc_lem.utilities.ai.ai_helper import _loads_json_object
    from cqc_lem.utilities.ai.client import client

    numbered = "\n".join(f"{n}. {c}" for n, c in enumerate(candidates, 1))
    try:
        response = client.chat.completions.create(
            model="lem-simple",
            messages=[{"role": "user", "content": _QUOTE_PICK_PROMPT.format(
                thesis=thesis or "(not stated)", numbered=numbered)}],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=2000,
        )
        parsed = _loads_json_object(response.choices[0].message.content or "")
        index = parsed.get("index") if isinstance(parsed, dict) else None
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError(f"no integer index in {json.dumps(parsed)[:80]}")
        if not 1 <= index <= len(candidates):
            raise ValueError(f"index {index} out of range")
        return candidates[index - 1], "llm"
    except Exception as e:
        # Expected degradation: the deterministic ranking already ordered them.
        log_debug("Quote tiebreak unavailable — keeping the deterministic best", error=str(e),
                  user_id=user_id, action_type="post_treatment")
        return candidates[0], "deterministic"


# --- One post's whole rhythm ------------------------------------------------------------------

def rhythm_history(receipts: Sequence[Mapping[str, Any]]) -> dict[str, list]:
    """Each rhythm dimension's recent values, most recent first, read off post receipts.

    A receipt from before this rotation carries no ``rhythm``: it was always the composite, so it
    reads as a ``typeset_card`` on the charcoal panel with its concept's layout and shot (or as
    ``photo_only`` when it had no headline) — the rotation starts from the real recent past.

    Args:
        receipts: Post brief receipts, most recent first.

    Returns:
        ``{dimension: [value or None, ...]}`` for ``RHYTHM_DIMENSIONS`` plus ``cast`` (dicts).
    """
    history: dict[str, list] = {dim: [] for dim in RHYTHM_DIMENSIONS}
    history["cast"] = []
    for receipt in receipts:
        concept = receipt.get("concept") if isinstance(receipt.get("concept"), dict) else {}
        rhythm = receipt.get("rhythm") if isinstance(receipt.get("rhythm"), dict) else None
        if rhythm is None:
            composite = bool(receipt.get("hook_text"))
            rhythm = {"treatment": TREATMENT_TYPESET_CARD if composite else TREATMENT_PHOTO_ONLY,
                      "layout": concept.get("layout") if composite else None,
                      "panel": "charcoal" if composite else None,
                      "shot": concept.get("shot") or None, "grade": None}
        if "setting" not in rhythm and rhythm.get("treatment") in AI_SCENE_TREATMENTS:
            # A receipt from before the setting dimension: read it off what was briefed.
            rhythm = dict(rhythm, setting=setting_class(concept.get("setting"))
                          or setting_class(receipt.get("prompt")) or None)
        if "style" not in rhythm:
            # A receipt from before the style dimension: read it off what was rendered.
            rhythm = dict(rhythm, style=style_of(
                rhythm.get("treatment"),
                receipt.get("archetype_rendered") or concept.get("archetype"),
                concept.get("art_style"), receipt.get("gate_verdict") == "last_resort"))
        for dim in RHYTHM_DIMENSIONS:
            value = rhythm.get(dim)
            history[dim].append(str(value) if value else None)
        history["cast"].append(concept.get("cast") if isinstance(concept.get("cast"), dict)
                               else None)
    return history


@dataclass(frozen=True)
class PostRhythm:
    """Everything the rotation decided for one post before anything renders.

    Attributes:
        plan: The treatment chain.
        panel: The typeset panel variant.
        grade: The photo grade for an AI-rendered scene.
        layout: The split layout, after the sameness gate.
        shot: The people-scene framing, after the sameness gate ('' when none).
        quote_candidates: The deterministic pull-quote candidates (empty off opinion posts).
        opinion: Why the post reads as an opinion ('' when it does not).
        rerolled: The dimensions the sameness gate re-rolled.
        setting: The setting to brief an AI scene with when the gate re-rolled it ('' keeps
            Stage 1's own).
        last_style: The most recent post's visual style ('' when unknown).
        recent_card_layouts: The last-resort card layouts of recent posts, most recent first.
        recent_styles: The visual styles of recent posts, most recent first.
    """

    plan: TreatmentPlan
    panel: str
    grade: str
    layout: str
    shot: str
    quote_candidates: tuple[str, ...]
    opinion: str
    rerolled: tuple[str, ...]
    setting: str = ""
    last_style: str = ""
    recent_card_layouts: tuple = ()
    recent_styles: tuple = ()


def unavailable_treatments(concept: Any, byline: Optional[str], candidates: Sequence[str],
                           opinion: str) -> dict[str, str]:
    """``{treatment: reason}`` for every treatment this post cannot take, decided before rendering.

    Args:
        concept: Stage 1's concept, or None.
        byline: The author's name for a quote card.
        candidates: ``quote_candidates`` for the post.
        opinion: ``opinion_signal`` for the post.

    Returns:
        The reasons; ``photo_only`` and ``typeset_card`` are always available.
    """
    if concept is None:
        return {TREATMENT_DATA_CARD: "no Stage 1 concept",
                TREATMENT_QUOTE_CARD: "no Stage 1 concept"}
    reasons: dict[str, str] = {}
    graphic = getattr(concept, "graphic", None) or {}
    if not graphic.get("stat"):
        reasons[TREATMENT_DATA_CARD] = "no verified thesis stat"
    elif not getattr(concept, "hook_phrase", ""):
        reasons[TREATMENT_DATA_CARD] = "no headline to set beside the stat"
    if not opinion:
        reasons[TREATMENT_QUOTE_CARD] = "not an opinion post"
    elif not candidates:
        reasons[TREATMENT_QUOTE_CARD] = ("no quotable sentence (a complete sentence of at most "
                                         f"{QUOTE_MAX_CHARS} characters, no hashtag or link)")
    elif not (byline or "").strip():
        reasons[TREATMENT_QUOTE_CARD] = "no author byline to attribute the quote to"
    return reasons


def plan_post_rhythm(concept: Any, text: str, history: Mapping[str, Sequence[Any]],
                     card_share: float, panels: Sequence[str], *,
                     byline: Optional[str] = None) -> PostRhythm:
    """Decide the next post's treatment chain, panel, grade, layout and shot. Deterministic.

    Args:
        concept: Stage 1's concept (after its own layout and shot rotation), or None.
        text: The post.
        history: ``rhythm_history`` of the author's recent post receipts.
        card_share: The brand kit's typeset-card share.
        panels: The panel variants available in the author's brand.
        byline: The author's name for a quote card.

    Returns:
        The rhythm.
    """
    from cqc_lem.utilities.ai.image_brief import PHOTO_GRADES
    from cqc_lem.utilities.ai.image_concept import POST_LAYOUTS, SHOTS

    seed = getattr(concept, "thesis", "") or (text or "")[:120]
    opinion = opinion_signal(concept, text)
    candidates = tuple(quote_candidates(text, getattr(concept, "thesis", "") or "")
                       if opinion else ())
    styles = list(history.get("style", []))
    # The style rule's blocks come AFTER the post's own reasons: a treatment the post cannot take
    # keeps the reason that is about the post.
    unavailable = {**style_blocks(styles),
                   **unavailable_treatments(concept, byline, candidates, opinion)}
    plan = plan_treatments(history.get("treatment", []), card_share, unavailable, seed)
    rerolled = ["treatment"] if plan.rerolled else []
    panel, again = pick_dimension(tuple(panels) or ("charcoal",), history.get("panel", []),
                                  seed + "panel")
    rerolled += ["panel"] if again else []
    grade, again = pick_dimension(tuple(PHOTO_GRADES), history.get("grade", []), seed + "grade")
    rerolled += ["grade"] if again else []
    layout, again = gate_value(getattr(concept, "layout", "") or "", POST_LAYOUTS,
                               history.get("layout", []), seed + "layout")
    rerolled += ["layout"] if again else []
    shot, again = gate_value(getattr(concept, "shot", "") or "", SHOTS, history.get("shot", []),
                             seed + "shot")
    rerolled += ["shot"] if again else []
    setting = ""
    if concept is not None:
        setting, again = reroll_setting(getattr(concept, "setting", "") or "",
                                        history.get("setting", []), text)
        rerolled += ["setting"] if again else []
    return PostRhythm(plan=plan, panel=panel or "charcoal", grade=grade or "",
                      layout=layout or "", shot=shot or "", quote_candidates=candidates,
                      opinion=opinion, rerolled=tuple(rerolled), setting=setting,
                      last_style=(styles[0] if styles else None) or "",
                      recent_card_layouts=tuple(history.get("card_layout", [])),
                      recent_styles=tuple(v for v in styles if v))
