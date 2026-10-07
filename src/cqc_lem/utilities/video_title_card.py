"""The $0 branded motion title card — the DEFAULT video when the AI source frame is refused.

Showcase round 4: a video post whose source frame the judge rejected fell straight to Pexels stock,
and stock matched the post's WORDS rather than its idea — "model retirement" came back as an
elderly couple in a snowy doorway, and a VARCHAR story got two hands reaching for each other. Both
read as automated. The fallback is now this card, drawn by code:

- the brand ground (charcoal, or off-white), the post's hook in Montserrat — gold on charcoal —
  COMPLETE on the first frame (LinkedIn's thumbnail; showcase round 5), the kicker, a gold rule
  under it and the byline, in one of three rotated layouts (``TITLE_CARD_VARIANTS``);
- a slow drift of one large brand slab behind the type and a rule that extends, so the clip moves
  for its whole length while every word stays put;
- 6-8 seconds (``VIDEO_TITLE_CARD_SECONDS``) at the tier's ratio (1:1 standard, 9:16 premium),
  piped frame by frame from Pillow into ffmpeg's libx264 — no drawtext, libass or system font;
- stored and captioned exactly like any other video (the caller's store path), named
  ``title_card_*`` so telemetry reads it as its own tier, never as Runway or stock.

The hook is the post's own words, never authored here: Stage 1's hook when the post has one,
else a COMPLETE first sentence or clause of the post (``video_captions.caption_candidates``).
The lower band of the frame is left clear for the burned caption, so the two never collide.

Pexels survives only behind ``VIDEO_PEXELS_FALLBACK_ENABLED`` (default off), and then only with a
query built from Stage 1's concrete visual anchors — never the post's raw words, and never an
anchor carrying a known stock homonym (``STOCK_HOMONYMS``). With no safe query there is no stock.
"""

import hashlib
import os
import re
import secrets
import subprocess
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from cqc_lem.utilities.env_constants import isTrue
from cqc_lem.utilities.logger import log_debug, log_info, log_warning

TASK_NAME = "video_title_card"

TITLE_CARD_PREFIX = "title_card_"
TITLE_CARD_FPS = 24
TITLE_CARD_MIN_SECONDS, TITLE_CARD_MAX_SECONDS = 6.0, 8.0
TITLE_CARD_SIZES = {"1:1": (1080, 1080), "9:16": (1080, 1920)}
GROUND_CHARCOAL, GROUND_OFF_WHITE = "charcoal", "off_white"
GROUNDS = (GROUND_CHARCOAL, GROUND_OFF_WHITE)
# The longest post-derived hook the card sets (a complete clause; never cut to fit).
TITLE_HOOK_MAX_CHARS = 110
# The lower share of the frame the card keeps clear for the burned caption band.
CAPTION_CLEARANCE = 0.30

# Words whose stock-search sense is NOT the business sense the post means. A stock query carrying
# one returns the literal pun: "retirement" is pensioners, "model" a fashion shoot.
STOCK_HOMONYMS = frozenset({
    "retirement", "retire", "retired", "model", "models", "pipeline", "pipelines", "cloud",
    "clouds", "bug", "bugs", "token", "tokens", "agent", "agents", "spam", "stack", "stacks",
    "crash", "crashes", "deploy", "deployment", "release", "releases", "branch", "branches",
    "tree", "trees", "string", "strings", "hands", "hand", "reach", "ladder", "funnel", "lead",
    "leads", "burn", "cold", "warm", "hot", "fire", "firewall", "window", "windows", "shell",
    "python", "java", "rust", "container", "containers", "queue", "queues", "cookie", "cookies",
    "memory", "drift", "gateway", "bridge", "key", "keys", "lock", "golden", "hour", "zero",
})


def pexels_fallback_enabled() -> bool:
    """``VIDEO_PEXELS_FALLBACK_ENABLED``, read at the call site so the toggle needs no restart."""
    return isTrue(os.environ.get("VIDEO_PEXELS_FALLBACK_ENABLED", "False"))


def title_card_seconds() -> float:
    """``VIDEO_TITLE_CARD_SECONDS`` clamped to the 6-8 s the card is designed for."""
    try:
        seconds = float(os.environ.get("VIDEO_TITLE_CARD_SECONDS", "7.0"))
    except ValueError:
        seconds = 7.0
    return min(TITLE_CARD_MAX_SECONDS, max(TITLE_CARD_MIN_SECONDS, seconds))


def stock_query_from_concept(concept: Any) -> Optional[str]:
    """A Pexels query built ONLY from Stage 1's concrete visual anchors, or None.

    The literal-pun guard: the post's raw words are never the query, and an anchor carrying a
    ``STOCK_HOMONYMS`` word is dropped whole ("model retirement cron" is not three objects, it is
    a pun waiting to happen). At most two anchors, so the search stays specific.

    Args:
        concept: Stage 1's ``ImageConcept``, or None.

    Returns:
        The query, or None when no safe anchor survives (no stock at all).
    """
    if concept is None:
        return None
    from cqc_lem.utilities.ai.image_brief import person_word, usable_anchors

    safe = []
    for anchor in usable_anchors(concept):
        words = {w.strip(".,;:!?'\"").lower() for w in str(anchor).split()}
        if not words or words & STOCK_HOMONYMS or person_word(anchor):
            continue
        safe.append(str(anchor).strip())
        if len(safe) == 2:
            break
    return " ".join(safe) or None


# How many leading sentences the card may skip to find an opening without an unvouched figure.
_HOOK_SKIP_SENTENCES = 3


def title_card_hook(text: Optional[str], concept: Any = None) -> Optional[str]:
    """The words the card sets — the post's own, never authored. None when there are none.

    Stage 1's hook (already gated for grammar and truth) when the post has one; else the post's
    complete first sentence or leading clause of at most ``TITLE_HOOK_MAX_CHARS``.

    Args:
        text: The post.
        concept: Stage 1's concept, or None.

    Returns:
        The hook, or None.
    """
    hook = str(getattr(concept, "hook_phrase", "") or "").strip()
    if hook:
        return hook
    from cqc_lem.utilities.ai.fact_consistency import prints_any, unprovenanced_figures
    from cqc_lem.utilities.video_captions import caption_candidates

    # Showcase round 7: slot_141's opening "5.6 hours per week" was unsourced. The card sets the
    # first candidate that prints no figure the post does not vouch for.
    # When every candidate prints one, the first still sets (a card beats a missing asset) — the
    # post's own hook-provenance finding holds it for review.
    unsourced = tuple(getattr(concept, "unsourced_figures", ()) or ())
    candidates = caption_candidates(text, TITLE_HOOK_MAX_CHARS)
    rest = text or ""
    for _ in range(_HOOK_SKIP_SENTENCES + 1):
        clean = [c for c in caption_candidates(rest, TITLE_HOOK_MAX_CHARS)
                 if not unprovenanced_figures(c, text) and not prints_any(c, unsourced)]
        if clean:
            return clean[0]
        parts = re.split(r"(?<=[.!?])\s+", rest.strip(), maxsplit=1)
        if len(parts) < 2:
            break
        rest = parts[1]
    return (candidates or [None])[0]


# The headline each card set, keyed by the card's file name — read by the caption burn (same task)
# so the caption never repeats it. Bounded and process-local, like the supplied-material registry.
_CARD_HEADLINES: dict = {}
_CARD_HEADLINES_MAX = 64


def remember_headline(path: str, headline: str) -> None:
    """Record the headline a rendered card set, keyed by its file name."""
    key = os.path.basename(path or "")
    if not key or not headline:
        return
    while len(_CARD_HEADLINES) >= _CARD_HEADLINES_MAX:
        _CARD_HEADLINES.pop(next(iter(_CARD_HEADLINES)))
    _CARD_HEADLINES[key] = headline


def card_headline(path: Optional[str], text: Optional[str] = None) -> Optional[str]:
    """The headline a title-card video shows, or None for any other video.

    The recorded one when this process rendered the card; otherwise, for a ``title_card_*`` file,
    the hook the card would have set from the post (`title_card_hook`). Never raises.

    Args:
        path: The video file (its name survives the copy into the assets volume).
        text: The post, for a card this process did not render.

    Returns:
        The headline, or None.
    """
    name = os.path.basename(path or "")
    if name in _CARD_HEADLINES:
        return _CARD_HEADLINES[name]
    if not name.startswith(TITLE_CARD_PREFIX):
        return None
    try:
        return title_card_hook(text)
    except Exception as e:
        log_debug("Title card headline unavailable for the caption", error=str(e),
                  task_name=TASK_NAME)
        return None


def pick_ground(seed: str) -> str:
    """Charcoal or off-white, stable per piece so a re-render draws the same card."""
    digest = hashlib.sha256((seed or "").encode("utf-8")).digest()
    return GROUNDS[digest[0] % len(GROUNDS)]


@dataclass(frozen=True)
class TitleCardPalette:
    """The card's colours as RGB tuples."""

    ground: tuple
    slab: tuple
    hook: tuple
    kicker: tuple
    rule: tuple
    byline: tuple


def title_card_palette(brand: Any = None, ground: str = GROUND_CHARCOAL) -> TitleCardPalette:
    """The card's colours in the author's brand.

    On charcoal the hook is the brand gold. Gold type cannot reach 4.5:1 on off-white, so there the
    hook is charcoal and the gold carries the rule and the slab instead; the kicker keeps the dark
    gold only where it still reads at 3:1.

    Args:
        brand: An ``image_compose.BrandStyle``; the reference brand by default.
        ground: ``GROUND_CHARCOAL`` or ``GROUND_OFF_WHITE``.

    Returns:
        The palette.
    """
    from cqc_lem.utilities.ai.image_compose import BrandStyle, _hex, contrast_ratio

    brand = brand or BrandStyle()
    dark, light = _hex(brand.neutral_dark), _hex(brand.neutral_light)
    gold, accent = _hex(brand.primary), _hex(brand.accent)

    def mix(a: tuple, b: tuple, t: float) -> tuple:
        return tuple(round(x + (y - x) * t) for x, y in zip(a, b))

    if ground == GROUND_OFF_WHITE:
        kicker = (accent if contrast_ratio(brand.accent, brand.neutral_light) >= 3.0 else dark)
        return TitleCardPalette(ground=light, slab=mix(light, gold, 0.22), hook=dark,
                                kicker=kicker, rule=gold, byline=mix(dark, light, 0.25))
    return TitleCardPalette(ground=dark, slab=mix(dark, accent, 0.16), hook=gold, kicker=accent
                            if contrast_ratio(brand.accent, brand.neutral_dark) >= 3.0 else gold,
                            rule=gold, byline=mix(light, dark, 0.15))


@dataclass(frozen=True)
class _Word:
    text: str
    x: int
    y: int


# Showcase round 5: LinkedIn shows a video's FIRST frame as its thumbnail, and the card revealed
# its hook word by word — slot_123/135/141 sat in the feed as a lone "AI", "What" and "Is". Every
# variant now draws its COMPLETE hook on frame 0; only secondary elements move (the brand slab
# drifts, the rule extends). And three layouts rotate from the author's own card history, so two
# consecutive videos never open on the same composition.
VARIANT_POSTER = "statement_poster"   # the whole hook at poster scale, left-aligned
VARIANT_NUMBER = "number_led"         # the hook's LEADING figure as the hero, the rest beneath
VARIANT_QUESTION = "question_kicker"  # the kicker on a gold tag, the hook centred beneath it
TITLE_CARD_VARIANTS = (VARIANT_POSTER, VARIANT_NUMBER, VARIANT_QUESTION)
_VARIANT_HISTORY = 6
# How far the rule extends over the clip, as a share of its frame-0 length.
_RULE_GROWTH = 0.35


@dataclass(frozen=True)
class TitleCardLayout:
    """Everything a frame needs, decided once: sizes and positions. Nothing is hidden at t=0."""

    size: tuple
    palette: TitleCardPalette
    hook_font: Any
    hook_size: int
    words: tuple
    rule_box: tuple
    kicker: str
    kicker_font: Any
    kicker_xy: tuple
    byline: str
    byline_font: Any
    byline_xy: tuple
    lines: tuple
    seconds: float
    slab_top: int = 0
    variant: str = VARIANT_POSTER
    hero: str = ""
    hero_font: Any = None
    hero_xy: tuple = (0, 0)
    kicker_box: tuple = ()


def _medium_font(size: int) -> Any:
    from PIL import ImageFont

    from cqc_lem.utilities.ai.image_compose import FONT_DIR, load_font

    path = os.path.join(str(FONT_DIR), "Montserrat-Medium.ttf")
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return load_font(size)


def available_variants(hook: Optional[str]) -> tuple:
    """The layouts this hook can take: ``number_led`` only when a figure LEADS it.

    Args:
        hook: The words the card sets.

    Returns:
        The variants, in ``TITLE_CARD_VARIANTS`` order.
    """
    from cqc_lem.utilities.ai.image_compose import split_hero

    hero, rest = split_hero(hook or "")
    return tuple(v for v in TITLE_CARD_VARIANTS
                 if v != VARIANT_NUMBER or (hero and len(rest.split()) >= 2))


def pick_variant(hook: Optional[str], recent: Sequence, seed: str = "") -> str:
    """The layout for the next card: least-recently-used, so it never repeats the last one.

    A question hook prefers ``question_kicker`` when that was not the last card's layout.

    Args:
        hook: The words the card sets.
        recent: Recent cards' layouts, most recent first.
        seed: Stable per-card text for the no-history case.

    Returns:
        One of ``TITLE_CARD_VARIANTS``.
    """
    from cqc_lem.utilities.ai.post_treatment import lru_order

    options = available_variants(hook)
    recent = [r for r in recent if r]
    if (hook or "").rstrip().endswith("?") and (not recent or recent[0] != VARIANT_QUESTION):
        return VARIANT_QUESTION
    return lru_order(options, recent, seed)[0]


def variant_history_path(user_id: int) -> str:
    """Where one author's recent card layouts are kept (inside the assets volume)."""
    return os.path.join(title_card_dir(), "history", f"{int(user_id)}.json")


def recent_cards(user_id: Optional[int]) -> list:
    """This author's recent title cards as ``(layout, ground)``, most recent first. Never raises.

    Read from disk, not process state, so a rotation spans workers and restarts. An entry written
    before showcase round 7 is a bare layout name; its ground reads as None (unknown).

    Args:
        user_id: The author; None reads nothing.

    Returns:
        The cards ([] when unknown or unreadable).
    """
    import json

    if user_id is None:
        return []
    try:
        with open(variant_history_path(user_id), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for entry in data:
        variant = entry.get("variant") if isinstance(entry, dict) else entry
        ground = entry.get("ground") if isinstance(entry, dict) else None
        if variant in TITLE_CARD_VARIANTS:
            out.append((variant, ground if ground in GROUNDS else None))
    return out


def recent_variants(user_id: Optional[int]) -> list:
    """This author's recent title-card layouts, most recent first ([] when unknown). Never raises."""
    return [variant for variant, _ground in recent_cards(user_id)]


def pick_card_style(hook: Optional[str], recent: Sequence, seed: str = "") -> tuple:
    """The next card's ``(layout, ground)``: never a pair from the last ``PAIR_WINDOW`` cards.

    Showcase round 7: slot_123 and slot_135 were the same layout on the same charcoal. The layout
    rotates least-recently-used (``pick_variant``); the ground was a hash of the post, so two
    cards could land on one pair. It now joins the rotation: the pick's ground unless that pair
    ran within the window, then the other ground, then another layout
    (``post_treatment.fresh_pair``).

    Args:
        hook: The words the card sets.
        recent: ``recent_cards`` — ``(layout, ground)``, most recent first.
        seed: Stable per-card text.

    Returns:
        ``(layout, ground)``.
    """
    from cqc_lem.utilities.ai.post_treatment import fresh_pair

    recent = list(recent)
    variant = pick_variant(hook, [v for v, _g in recent], seed)
    ground = pick_ground(seed)
    grounds = (ground, *[g for g in GROUNDS if g != ground])
    variant, ground, _changed = fresh_pair(variant, ground, available_variants(hook), grounds,
                                           [v for v, _g in recent], [g for _v, g in recent])
    return variant, ground


def record_variant(user_id: Optional[int], variant: str, ground: Optional[str] = None) -> None:
    """Prepend the card that just rendered to the author's card history. Never raises.

    A lost write costs one repeated layout at most, so it logs at DEBUG.

    Args:
        user_id: The author; None records nothing.
        variant: The layout that just rendered.
        ground: Its ground (charcoal or off-white), when known.
    """
    import json

    if user_id is None:
        return
    path = variant_history_path(user_id)
    history = [{"variant": v, "ground": g}
               for v, g in [(variant, ground), *recent_cards(user_id)]][:_VARIANT_HISTORY]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(history, handle)
    except OSError as e:
        log_debug("Title card layout history not written", error=str(e), user_id=user_id,
                  task_name=TASK_NAME)


def _fit_lines(draw: Any, text: str, width: int, height: int, high: int, low: int,
               max_lines: int) -> Optional[tuple]:
    """``(font, size, lines, line_h)`` for the largest size ``text`` fits at, or None."""
    from cqc_lem.utilities.ai.image_compose import _line_height, _wrap, load_font

    for size_px in range(high, low - 1, -2):
        font = load_font(size_px)
        lines = _wrap(draw, text.split(), font, width)
        line_h = _line_height(font, size_px)
        if lines and len(lines) <= max_lines and line_h * len(lines) <= height:
            return font, size_px, lines, line_h
    return None


def plan_title_card(hook: str, *, size: tuple, palette: TitleCardPalette, kicker: str = "",
                    byline: str = "", seconds: float = 7.0,
                    variant: str = VARIANT_POSTER) -> TitleCardLayout:
    """Fit the hook and place every element. Raises ``ValueError`` when the hook cannot be set.

    Args:
        hook: The words to set.
        size: ``(width, height)``.
        palette: The colours.
        kicker: The uppercase topic tag ('' for none).
        byline: The author's name ('' for none).
        seconds: The clip length.
        variant: One of ``TITLE_CARD_VARIANTS``; ``number_led`` without a leading figure is set
            as the poster.

    Returns:
        The layout.
    """
    from PIL import Image, ImageDraw

    from cqc_lem.utilities.ai.image_compose import load_font, split_hero
    from cqc_lem.utilities.ai.image_concept import tidy_figures

    hook = " ".join(tidy_figures(hook).split())
    if not hook:
        raise ValueError("a title card needs a hook")
    width, height = size
    margin = round(width * 0.09)
    draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    usable_w = width - 2 * margin
    top_zone = round(height * 0.17)
    # Showcase round 6: the byline sat UNDER the burned caption band on both title-card videos.
    # The floor is now the higher of the reserved clearance and the band's worst-case top, and the
    # rule + byline are reserved INSIDE it (`tail` below), so nothing the card draws can land there.
    from cqc_lem.utilities.video_captions import caption_band_top

    floor_y = min(round(height * (1 - CAPTION_CLEARANCE)),
                  caption_band_top((width, height))) - round(height * 0.08)
    usable_h = floor_y - top_zone
    kicker = (kicker or "").upper().strip()
    kicker_font = load_font(max(18, round(width * 0.03)))
    kicker_xy, kicker_box = (margin, round(height * 0.09)), ()
    hero, hero_font, hero_xy = "", None, (0, 0)
    centred = False
    text = hook
    rest = hook  # what sets beneath the hero; the whole hook on every non-number path
    if variant == VARIANT_NUMBER:
        hero, rest = split_hero(hook)
        if not hero or len(rest.split()) < 2:
            variant, hero = VARIANT_POSTER, ""
    if variant == VARIANT_NUMBER:
        hero_fit = _fit_lines(draw, hero, usable_w, round(usable_h * 0.5), round(width * 0.26),
                              round(width * 0.12), 1)
        if hero_fit is None:
            variant, hero = VARIANT_POSTER, ""
        else:
            hero_font, hero_size, _hero_lines, hero_h = hero_fit
            hero_xy = (margin - draw.textbbox((0, 0), hero, font=hero_font)[0], top_zone)
            text = rest
            top_zone += hero_h + round(height * 0.01)
            usable_h = floor_y - top_zone
    if variant == VARIANT_QUESTION:
        centred = True
        if kicker:
            pad = round(width * 0.018)
            kw = draw.textlength(kicker, font=kicker_font)
            kh = kicker_font.size
            left = round((width - kw) / 2) - pad
            top = round(height * 0.11)
            kicker_box = (left, top - pad, round(left + kw + 2 * pad), top + kh + pad)
            kicker_xy = (left + pad, top - round(kh * 0.1))
            top_zone = max(top_zone, kicker_box[3] + round(height * 0.05))
            usable_h = floor_y - top_zone
    high = round(width * (0.08 if variant == VARIANT_NUMBER else 0.105))
    byline = (byline or "").strip()
    byline_font = _medium_font(max(18, round(width * 0.034)))
    rule_h = max(6, round(width * 0.008))
    byline_gap = round(width * 0.035)
    byline_top_row = False

    def tail(line_h: int, with_byline: bool) -> int:
        # What sets beneath the hook: the gap, the rule and — unless it moved up — the byline.
        below = round(line_h * 0.35) + rule_h
        return below + (byline_gap + round(byline_font.size * 1.25) if with_byline else 0)

    from cqc_lem.utilities.ai.image_compose import _line_height

    worst_line = _line_height(load_font(high), high)
    fitted = _fit_lines(draw, text, usable_w, usable_h - tail(worst_line, bool(byline)), high,
                        round(width * 0.05), 5)
    if fitted is None and byline:
        # No room for the byline under the hook: it moves into the top row instead, never under
        # the caption band.
        byline_top_row = True
        fitted = _fit_lines(draw, text, usable_w, usable_h - tail(worst_line, False), high,
                            round(width * 0.05), 5)
    if fitted is None:
        # The pre-round-6 fit, with the byline (if any) already moved up: a hook that set before
        # still sets, and only the short rule can sit in the floor's margin.
        byline_top_row = bool(byline)
        fitted = _fit_lines(draw, text, usable_w, usable_h, high, round(width * 0.05), 5)
    if fitted is None:
        raise ValueError("the hook does not fit the title card legibly")
    font, size_px, lines, line_h = fitted
    block_h = line_h * len(lines)
    stack_h = block_h + tail(line_h, bool(byline) and not byline_top_row)
    y0 = top_zone + (0 if variant == VARIANT_NUMBER else max(0, (usable_h - stack_h) // 2))
    words: list[_Word] = []
    space = draw.textlength(" ", font=font)
    for row, line in enumerate(lines):
        line_w = draw.textlength(line, font=font)
        start = (width - line_w) / 2 if centred else margin
        x = start - draw.textbbox((0, 0), line, font=font)[0]
        for word in line.split():
            words.append(_Word(word, round(x), y0 + row * line_h))
            x += draw.textlength(word, font=font) + space
    rule_y = y0 + block_h + round(line_h * 0.35)
    rule_w = round(width * 0.22)
    rule_x = round((width - rule_w) / 2) if centred else margin
    rule_box = (rule_x, rule_y, rule_x + rule_w, rule_y + rule_h)
    if byline_top_row:
        # Right-aligned above the kicker row, clear of a centred kicker tag.
        byline_xy = (round(width - margin - draw.textlength(byline, font=byline_font)),
                     round(height * 0.035))
    else:
        byline_x = (round((width - draw.textlength(byline, font=byline_font)) / 2) if centred
                    else margin)
        byline_xy = (byline_x, rule_y + rule_h + byline_gap)
    # The slab lives BELOW the type block, so it never sits under a word.
    slab_top = min(round(height * 0.80), max(round(height * 0.62),
                                             byline_xy[1] + round(height * 0.08)))
    return TitleCardLayout(size=(width, height), palette=palette, hook_font=font,
                           hook_size=size_px, words=tuple(words), rule_box=rule_box,
                           kicker=kicker, kicker_font=kicker_font, kicker_xy=kicker_xy,
                           byline=byline, byline_font=byline_font, byline_xy=byline_xy,
                           lines=tuple(lines), seconds=seconds, slab_top=slab_top,
                           variant=variant, hero=hero, hero_font=hero_font, hero_xy=hero_xy,
                           kicker_box=kicker_box)


def _ease(p: float) -> float:
    p = min(1.0, max(0.0, p))
    return 1 - (1 - p) ** 3


def render_title_card_frame(layout: TitleCardLayout, t: float) -> Any:
    """One frame at ``t`` seconds, as a PIL RGB image — complete at ``t=0`` (the thumbnail).

    Args:
        layout: ``plan_title_card`` output.
        t: Seconds from the start.

    Returns:
        The frame.
    """
    from PIL import Image, ImageDraw

    pal = layout.palette
    width, height = layout.size
    image = Image.new("RGB", (width, height), pal.ground)
    draw = ImageDraw.Draw(image)
    # The slab drifts left across the whole clip: the card moves for its whole length. It sits
    # below the type block with a gold edge, under where the caption band will burn.
    drift = t / max(0.1, layout.seconds)
    sx = round(width * (0.46 - 0.12 * drift))
    sy = (layout.slab_top or round(height * 0.66)) + round(height * 0.02 * (1 - drift))
    edge = max(6, round(width * 0.008))
    draw.rectangle((sx, sy, width, height), fill=pal.slab)
    draw.rectangle((sx, sy, width, sy + edge - 1), fill=pal.rule)
    if layout.kicker:
        if layout.kicker_box:
            draw.rectangle(layout.kicker_box, fill=pal.rule)
            # The darker of the two inks reads on the gold tag on either ground.
            ink = min(pal.ground, pal.hook, key=sum)
            draw.text(layout.kicker_xy, layout.kicker, font=layout.kicker_font, fill=ink)
        else:
            draw.text(layout.kicker_xy, layout.kicker, font=layout.kicker_font, fill=pal.kicker)
    if layout.hero and layout.hero_font is not None:
        draw.text(layout.hero_xy, layout.hero, font=layout.hero_font, fill=pal.hook)
    for word in layout.words:
        draw.text((word.x, word.y), word.text, font=layout.hook_font, fill=pal.hook)
    left, top, right, bottom = layout.rule_box
    grow = 1 + _RULE_GROWTH * _ease(t / max(0.1, layout.seconds))
    if layout.variant == VARIANT_QUESTION:
        centre, half = (left + right) / 2, (right - left) * grow / 2
        draw.rectangle((round(centre - half), top, round(centre + half), bottom), fill=pal.rule)
    else:
        draw.rectangle((left, top, left + round((right - left) * grow), bottom), fill=pal.rule)
    if layout.byline:
        draw.text(layout.byline_xy, layout.byline, font=layout.byline_font, fill=pal.byline)
    return image


def _ffmpeg() -> Optional[str]:
    import shutil

    return shutil.which("ffmpeg")


def write_title_card_video(layout: TitleCardLayout, out_path: str, *,
                           fps: int = TITLE_CARD_FPS, timeout: int = 300) -> bool:
    """Encode the card to ``out_path`` (H.264 MP4, silent). True only when the file is real.

    Frames are piped as raw RGB into one ffmpeg process. A missing ffmpeg, a broken pipe, a
    non-zero exit or an empty output all return False and leave no partial file behind.

    Args:
        layout: ``plan_title_card`` output.
        out_path: The MP4 to write.
        fps: Frames per second.
        timeout: Seconds ffmpeg may take to finish after the last frame.

    Returns:
        Whether a usable MP4 was written.
    """
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        log_debug("ffmpeg is not installed — no title card", task_name=TASK_NAME)
        return False
    width, height = layout.size
    command = [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{width}x{height}", "-r", str(fps), "-i", "-", "-an", "-c:v", "libx264",
               "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", out_path]
    frames = max(1, round(layout.seconds * fps))
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE)
        try:
            for index in range(frames):
                process.stdin.write(render_title_card_frame(layout, index / fps).tobytes())
            # communicate() flushes and closes stdin itself, then waits for the encoder.
            _out, err = process.communicate(timeout=timeout)
        except (OSError, ValueError, subprocess.SubprocessError):
            process.kill()
            _out, err = process.communicate()
    except (OSError, subprocess.SubprocessError) as e:
        log_warning("Title card encode could not run — no title card", exc=e, task_name=TASK_NAME)
        _remove(out_path)
        return False
    if process.returncode != 0:
        # A fixed template, the stderr tail at DEBUG: a per-file tail would never repeat a key.
        log_warning("Title card encode failed — no title card", task_name=TASK_NAME)
        log_debug(f"ffmpeg title card stderr: {(err or b'')[-300:]!r}", task_name=TASK_NAME)
        _remove(out_path)
        return False
    try:
        if os.path.getsize(out_path) <= 0:
            raise OSError("empty output")
    except OSError as e:
        log_warning("Title card encode produced no usable file — no title card", exc=e,
                    task_name=TASK_NAME)
        _remove(out_path)
        return False
    return True


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        # Nothing was written, or it is already gone: the caller is on a failure path either way.
        pass


def title_card_dir() -> str:
    """Where title cards are rendered — inside the assets volume, so the store path may move them."""
    from cqc_lem import assets_dir

    return os.path.join(assets_dir, "videos", "title_card")


def _brand_for(user_id: Optional[int]) -> Any:
    from cqc_lem.utilities.ai.image_compose import brand_style

    try:
        from cqc_lem.utilities.brand_kit import brand_clause_for_user
        return brand_style(brand_clause_for_user(user_id) if user_id else None)
    except Exception as e:
        # The reference brand is still the brand: no reason to lose the card.
        log_debug("Brand kit unreadable for the title card — reference brand", error=str(e),
                  user_id=user_id, task_name=TASK_NAME)
        return brand_style(None)


def create_title_card_video(text: Optional[str], *, user_id: Optional[int] = None,
                            post_id: Optional[int] = None, concept: Any = None,
                            ratio: str = "1:1", byline: Optional[str] = None) -> Optional[str]:
    """Render the post's branded title card and return the local MP4 path, or None. Never raises.

    Args:
        text: The post.
        user_id: The author — their brand colours.
        post_id: For the file name and log context.
        concept: Stage 1's concept (its hook and kicker), or None.
        ratio: ``"1:1"`` or ``"9:16"``, matching the tier the clip replaces.
        byline: The author's name.

    Returns:
        A path inside the assets volume named ``title_card_*.mp4``, or None.
    """
    try:
        hook = title_card_hook(text, concept)
        if not hook:
            log_info("No complete hook to set on a title card", user_id=user_id, post_id=post_id,
                     task_name=TASK_NAME)
            return None
        from cqc_lem.utilities.ai.image_concept import concept_kicker
        from cqc_lem.utilities.utils import create_folder_if_not_exists

        variant, ground = pick_card_style(hook, recent_cards(user_id), f"{post_id}:{hook}")
        palette = title_card_palette(_brand_for(user_id), ground)
        kicker = concept_kicker(concept)
        if variant == VARIANT_QUESTION and not kicker:
            from cqc_lem.utilities.ai.image_concept import derive_kicker

            kicker = derive_kicker(text or hook)
        layout = plan_title_card(hook, size=TITLE_CARD_SIZES.get(ratio, TITLE_CARD_SIZES["1:1"]),
                                 palette=palette, kicker=kicker, byline=byline or "",
                                 seconds=title_card_seconds(), variant=variant)
        directory = title_card_dir()
        create_folder_if_not_exists(directory)
        out_path = os.path.join(directory,
                                f"{TITLE_CARD_PREFIX}{post_id or 0}_{secrets.token_hex(6)}.mp4")
        if not write_title_card_video(layout, out_path):
            return None
        remember_headline(out_path, hook)
        record_variant(user_id, layout.variant, ground)
        log_info("Rendered the branded title card video", user_id=user_id, post_id=post_id,
                 task_name=TASK_NAME, ratio=ratio, variant=layout.variant)
        return out_path
    except Exception as e:
        # The card is the fallback's fallback: a fault here is a defect worth an alert, and the
        # post then goes to the missing-asset gate exactly as a failed render always did.
        log_warning("Title card video raised — the video post has no asset", exc=e,
                    user_id=user_id, post_id=post_id, task_name=TASK_NAME)
        return None
