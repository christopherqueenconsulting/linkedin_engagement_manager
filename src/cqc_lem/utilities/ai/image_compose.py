"""The ONE place a headline meets a render (issue #2241, gauntlet round 6).

Every remaining cover defect came from one root cause: gpt-image DRAWS the headline, so the
panel drifted through charcoal, black, navy and grey-green, the type through gold, pale gold and
off-white at any weight, and three of nine headlines clipped the frame edge. A render now
carries no text at all, and this module typesets the hook onto it deterministically with PIL:

- the bundled Montserrat ExtraBold (SIL OFL, ``resources/fonts/``), falling back to the same
  system bold faces ``carousel_creator`` uses;
- the panel in the brand's neutral dark and the headline in the brand primary, EXACTLY;
- text wrapped to at most ``MAX_LINES`` lines and shrunk to fit a safe area with at least
  ``SAFE_MARGIN`` of the frame on every side, so legibility and clipping are guaranteed by
  construction — unit tests assert them instead of a vision judge guessing at them.

Layouts are compositing templates (``LAYOUTS``); Stage 1's rotation picks one per render.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cqc_lem.utilities.logger import log_info, log_warning

FONT_DIR = Path(__file__).resolve().parents[2] / "resources" / "fonts"
BRAND_FONT = FONT_DIR / "Montserrat-ExtraBold.ttf"
# The same system bold faces carousel_creator measures with, for a box without the bundled file.
_FALLBACK_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
)

# Every side keeps at least this fraction of the frame clear of text.
SAFE_MARGIN = 0.06
MAX_LINES = 3
# A covers line's cap height must stay legible at 400px wide: ~7% of the image height.
MIN_LINE_FRACTION = {"newsletter": 0.07, "post_image": 0.055}
# Round 7: the headline must read as a thumbnail — a CAP HEIGHT of at least 9% of the image
# height on a landscape cover, 6% on a 4:5 post. When the default backing cannot hold that in three
# lines, the panel widens / the band deepens a step at a time (``GROW_STEPS``) before giving up.
MIN_CAP_FRACTION = {"landscape": 0.09, "portrait": 0.06}
PANEL_WIDTHS = (0.38, 0.43, 0.48)
BAND_HEIGHTS = (0.26, 0.32, 0.38)
GROW_STEPS = len(PANEL_WIDTHS)
PANEL_WIDTH = PANEL_WIDTHS[0]
# The centred layouts on a landscape cover keep the headline in the central 60% of the width.
COVER_CENTRAL = 0.6
BAND_HEIGHT = BAND_HEIGHTS[0]
_LINE_SPACING = 1.12
_SCRIM_ALPHA = 190

PANEL_LEFT, PANEL_RIGHT = "panel_left", "panel_right"
BAND_TOP, LOWER_THIRD_BAND, FULL_BLEED = "band_top", "lower_third_band", "full_bleed"
# Round 8 (#2241): every overlay layout put type over a face or a torso, whatever the brief said.
# SPLIT layouts make that impossible by construction: the render (always square) is centre-cropped
# into ITS OWN region beside a solid type panel, on a canvas built at the surface's final size.
SPLIT_LEFT, SPLIT_RIGHT = "split_left", "split_right"
SPLIT_TOP, SPLIT_BOTTOM = "split_top", "split_bottom"
SPLIT_LAYOUTS = (SPLIT_LEFT, SPLIT_RIGHT, SPLIT_TOP, SPLIT_BOTTOM)
LAYOUTS = (PANEL_LEFT, PANEL_RIGHT, BAND_TOP, LOWER_THIRD_BAND, FULL_BLEED) + SPLIT_LAYOUTS
DEFAULT_LAYOUT = {"newsletter": SPLIT_LEFT, "post_image": SPLIT_TOP}
# The composited canvas per surface: a 16:9 cover, a 4:5 feed post (no later crop).
# Round 12: covers at 1920x1080, LinkedIn's recommended newsletter cover size (the square scene
# is upscaled LANCZOS into its region by ``cover_fit``).
CANVAS = {"newsletter": (1920, 1080), "post_image": (1080, 1350)}
# The type panel's share of the canvas, growing a step when the floor is not met.
SPLIT_PANEL = {"vertical": (0.40, 0.45, 0.50), "horizontal": (0.34, 0.40, 0.46)}
_SEAM_RULE = 4

# User 1's kit is the reference brand; any kit naming its own hexes overrides it.
DEFAULT_PRIMARY = "#E9D437"      # light gold — the headline
DEFAULT_NEUTRAL_DARK = "#1F1F1F"  # charcoal — the panel
DEFAULT_ACCENT = "#A89816"        # dark gold — the kicker, or the rule under the headline
DEFAULT_NEUTRAL_LIGHT = "#F7F5EF"  # off-white — the headline under a hero numeral, the byline


@dataclass(frozen=True)
class BrandStyle:
    """The exact colors a composite uses.

    Attributes:
        primary: Headline color, ``#RRGGBB``.
        neutral_dark: Panel / band / scrim color, ``#RRGGBB``.
        accent: The kicker (or, without one, the rule under the headline), ``#RRGGBB``.
        neutral_light: The headline under a hero numeral, and the byline, ``#RRGGBB``.
    """

    primary: str = DEFAULT_PRIMARY
    neutral_dark: str = DEFAULT_NEUTRAL_DARK
    accent: str = DEFAULT_ACCENT
    neutral_light: str = DEFAULT_NEUTRAL_LIGHT


_NAMED_HEX = re.compile(r"([A-Za-z][A-Za-z \-]{1,30}?)\s*\(?\s*(#[0-9A-Fa-f]{6})\b")
_NEUTRAL_NAMES = re.compile(r"charcoal|black|ink|graphite|slate|neutral[_\s-]?dark", re.IGNORECASE)
_PRIMARY_NAMES = re.compile(r"light gold|primary|gold", re.IGNORECASE)
_LIGHT_NAMES = re.compile(r"off[\s-]?white|cream|ivory|neutral[_\s-]?light|white", re.IGNORECASE)


def brand_style(brand_kit: Optional[str]) -> BrandStyle:
    """The exact colors from a brand clause's "name (#hex)" pairs, else the reference brand.

    Args:
        brand_kit: The pre-rendered brand clause, e.g. "light gold (#e9d437) and dark gold
            (#a89816) as accents against charcoal (#1f1f1f)".

    Returns:
        The style: neutral dark = the first charcoal/black-named hex, primary = the first
        gold/primary-named hex (light gold preferred), accent = a second gold.
    """
    pairs = [(name.strip().lower(), hexv.upper()) for name, hexv in
             _NAMED_HEX.findall(brand_kit or "")]
    neutral = next((h for n, h in pairs if _NEUTRAL_NAMES.search(n)), DEFAULT_NEUTRAL_DARK)
    golds = [(n, h) for n, h in pairs if _PRIMARY_NAMES.search(n)]
    primary = next((h for n, h in golds if "light" in n or "primary" in n),
                   golds[0][1] if golds else DEFAULT_PRIMARY)
    accent = next((h for n, h in golds if h != primary), DEFAULT_ACCENT)
    light = next((h for n, h in pairs if _LIGHT_NAMES.search(n)), DEFAULT_NEUTRAL_LIGHT)
    return BrandStyle(primary=primary, neutral_dark=neutral, accent=accent, neutral_light=light)


def load_font(size: int):
    """The bundled Montserrat ExtraBold at ``size``, else a system bold, else PIL's default.

    Args:
        size: Pixel size.

    Returns:
        A PIL font.
    """
    from PIL import ImageFont

    for path in (str(BRAND_FONT),) + _FALLBACK_FONTS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


@dataclass(frozen=True)
class Box:
    """A pixel rectangle: left, top, right, bottom."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        """Width in pixels."""
        return self.right - self.left

    @property
    def height(self) -> int:
        """Height in pixels."""
        return self.bottom - self.top


@dataclass(frozen=True)
class LayoutPlan:
    """Where the backing goes and where the text may go.

    Attributes:
        layout: The compositing template.
        backing: The solid panel/band box, or None for a scrim.
        scrim: The gradient scrim box for ``full_bleed``, or None.
        text_box: The safe area the text must fit inside.
        scene: For a split layout, the region the render fills (never under the type).
    """

    layout: str
    backing: Optional[Box]
    scrim: Optional[Box]
    text_box: Box
    scene: Optional[Box] = None


def _darkest_third(image, portrait: bool) -> int:
    """Index (0-2) of the darkest third — columns for landscape, rows for portrait."""
    gray = image.convert("L")
    w, h = gray.size
    means = []
    for i in range(3):
        box = (0, i * h // 3, w, (i + 1) * h // 3) if portrait else (i * w // 3, 0,
                                                                        (i + 1) * w // 3, h)
        region = gray.crop(box)
        hist = region.histogram()
        total = sum(hist) or 1
        means.append(sum(v * c for v, c in enumerate(hist)) / total)
    return min(range(3), key=means.__getitem__)


def _plan_split(size: tuple[int, int], layout: str, grow: int) -> LayoutPlan:
    """A solid type panel on one side, the scene region on the other — they never overlap."""
    w, h = size
    mx, my = round(w * SAFE_MARGIN), round(h * SAFE_MARGIN)
    if layout in (SPLIT_LEFT, SPLIT_RIGHT):
        pw = round(w * SPLIT_PANEL["vertical"][grow])
        pad = round(w * 0.03)
        if layout == SPLIT_LEFT:
            panel, scene = Box(0, 0, pw, h), Box(pw, 0, w, h)
            text = Box(mx, my, pw - pad, h - my)
        else:
            panel, scene = Box(w - pw, 0, w, h), Box(0, 0, w - pw, h)
            text = Box(w - pw + pad, my, w - mx, h - my)
        return LayoutPlan(layout, panel, None, text, scene)
    ph = round(h * SPLIT_PANEL["horizontal"][grow])
    pad = round(h * 0.025)
    if layout == SPLIT_TOP:
        panel, scene = Box(0, 0, w, ph), Box(0, ph, w, h)
        text = Box(mx, my, w - mx, ph - pad)
    else:
        panel, scene = Box(0, h - ph, w, h), Box(0, 0, w, h - ph)
        text = Box(mx, h - ph + pad, w - mx, h - my)
    return LayoutPlan(layout, panel, None, text, scene)


def cover_fit(image, box: "Box"):
    """``image`` scaled to COVER ``box`` and centre-cropped to it exactly.

    Args:
        image: The square render.
        box: The scene region.

    Returns:
        A new image of exactly ``box``'s size.
    """
    from PIL import Image

    iw, ih = image.size
    scale = max(box.width / iw, box.height / ih)
    resized = image.resize((max(box.width, round(iw * scale)), max(box.height, round(ih * scale))),
                           Image.LANCZOS)
    left = (resized.width - box.width) // 2
    top = (resized.height - box.height) // 2
    return resized.crop((left, top, left + box.width, top + box.height))


def plan_layout(size: tuple[int, int], layout: str, image=None, grow: int = 0) -> LayoutPlan:
    """The backing and the text safe area for ``layout`` on a ``size`` frame.

    Every text box keeps at least ``SAFE_MARGIN`` of the frame clear on every side.

    Args:
        size: ``(width, height)``.
        layout: One of ``LAYOUTS``; unknown values take ``panel_left``.
        image: The render, needed only by ``full_bleed`` to find its darkest third.
        grow: Which ``PANEL_WIDTHS`` / ``BAND_HEIGHTS`` step to use (0 = the default).

    Returns:
        The plan.
    """
    w, h = size
    grow = max(0, min(grow, GROW_STEPS - 1))
    if layout in SPLIT_LAYOUTS:
        return _plan_split(size, layout, grow)
    panel_width, band_height = PANEL_WIDTHS[grow], BAND_HEIGHTS[grow]
    mx, my = round(w * SAFE_MARGIN), round(h * SAFE_MARGIN)
    pad_x = round(w * 0.02)
    if layout == PANEL_RIGHT:
        panel = Box(w - round(w * panel_width), 0, w, h)
        return LayoutPlan(layout, panel, None,
                          Box(panel.left + pad_x, my, w - mx, h - my))
    if layout == BAND_TOP:
        band = Box(0, my, w, my + round(h * band_height))
        return LayoutPlan(layout, band, None,
                          Box(mx, band.top + round(h * 0.02), w - mx, band.bottom - round(h * 0.02)))
    if layout == LOWER_THIRD_BAND:
        band = Box(0, h - my - round(h * band_height), w, h - my)
        # A centred headline on a landscape cover stays in the central 60% of the width.
        left, right = ((round(w * (1 - COVER_CENTRAL) / 2), round(w * (1 + COVER_CENTRAL) / 2))
                       if w > h else (mx, w - mx))
        return LayoutPlan(layout, band, None,
                          Box(left, band.top + round(h * 0.02), right,
                              band.bottom - round(h * 0.02)))
    if layout == FULL_BLEED:
        portrait = h > w
        third = _darkest_third(image, portrait) if image is not None else 0
        if portrait:
            region = Box(0, third * h // 3, w, (third + 1) * h // 3)
            text = Box(mx, max(region.top, my), w - mx, min(region.bottom, h - my))
        else:
            region = Box(third * w // 3, 0, (third + 1) * w // 3, h)
            text = Box(max(region.left, mx), my, min(region.right, w - mx), h - my)
        return LayoutPlan(layout, None, region, text)
    panel = Box(0, 0, round(w * panel_width), h)
    return LayoutPlan(PANEL_LEFT, panel, None, Box(mx, my, panel.right - pad_x, h - my))


def cap_height(font) -> int:
    """The font's cap height in pixels — what a thumbnail reader actually sees."""
    left, top, right, bottom = font.getbbox("H")
    return bottom - top


# Round 7 (gauntlet, gpt-4.1 + blind critic): both judges scored every cover "a generic office" —
# the headline carried the topic alone. The cover is now an editorial SYSTEM, typeset by code:
# a KICKER (an uppercase topic tag above), a HERO NUMERAL (a grounded number on its own line at
# twice the headline size), the headline, and a quiet BYLINE at the bottom.
KICKER_RATIO = 0.35
HERO_RATIO = 2.0
_GAP_RATIO = 0.22
_TRACKING = 0.12
_NUMBER_TOKEN = re.compile(r"[$€£]?\d+(?:[.,]\d+)?(?:\s?%|[KkMmBb](?![A-Za-z])|x(?![A-Za-z]))?")


@dataclass(frozen=True)
class HeadlineParts:
    """What the cover typesets, top to bottom.

    Attributes:
        kicker: The uppercase topic tag, or ''.
        hero: The grounded number set as a hero numeral, or ''.
        rest: The headline (the hook, minus the hero numeral when there is one).
        signature: The byline, or ''.
    """

    kicker: str = ""
    hero: str = ""
    rest: str = ""
    signature: str = ""


def split_hero(hook: str) -> tuple[str, str]:
    """Split a hook into its hero numeral and the rest.

    Args:
        hook: The hook, e.g. "45% less engagement on AI posts".

    Returns:
        ``(hero, rest)`` — ``("45%", "Less engagement on AI posts")``; ``("", hook)`` when it
        carries no number, OR when the number does not LEAD the hook. #2241 showcase: "We saved
        $30K per quarter" became hero "$30K" over "We saved per quarter" — lifting a number out of
        mid-sentence leaves a hole. Only a leading number lifts cleanly ("45% less engagement…"
        → "Less engagement…"); any other hook keeps its whole sentence in the headline.
    """
    match = _NUMBER_TOKEN.search(hook or "")
    if not match or (hook or "")[:match.start()].strip(" \"'“‘([—–-:"):
        return "", sentence_case(hook)
    rest = f"{hook[:match.start()]} {hook[match.end():]}"
    rest = " ".join(rest.split()).strip(" ,:;-–—")
    rest = re.sub(r"\s+([,:;])", r"\1", rest)
    return match.group(0).replace(" ", ""), sentence_case(rest)


def headline_parts(hook: str, kicker: Optional[str] = None,
                   signature: Optional[str] = None, hero: bool = True) -> HeadlineParts:
    """The cover's typeset parts for a hook, its kicker and its byline.

    Args:
        hook: The headline.
        kicker: Stage 1's topic tag; uppercased here.
        signature: The byline.
        hero: Lift a number in the hook out as a hero numeral. A code-drawn graphic passes
            False: its own figure is the hero, and a second one in the panel would repeat it.

    Returns:
        The parts.
    """
    if not hero:
        return HeadlineParts(kicker=" ".join((kicker or "").upper().split()),
                             rest=sentence_case(hook),
                             signature=" ".join((signature or "").split()))
    hero, rest = split_hero(sentence_case(hook))
    return HeadlineParts(kicker=" ".join((kicker or "").upper().split()), hero=hero, rest=rest,
                         signature=" ".join((signature or "").split()))


@dataclass(frozen=True)
class HeadlineFit:
    """A fitted cover: the plan, every element's font and position, and how big it reads.

    Attributes:
        plan: The layout plan (after any growth step).
        text_box: The box the text block was fitted to.
        size: The headline font size; the hero is ``HERO_RATIO`` and the kicker ``KICKER_RATIO``
            of it.
        font: The headline font.
        lines: The wrapped headline lines.
        line_height: Pixels per headline line.
        cap: Headline cap height in pixels.
        floor: The cap-height floor for this orientation, in pixels.
        block_height: The whole kicker + hero + headline block's height.
    """

    plan: "LayoutPlan"
    text_box: Box
    size: int
    font: object
    lines: tuple
    line_height: int
    cap: int
    floor: int
    block_height: int


def sentence_case(hook: str) -> str:
    """Uppercase the first letter; every other character — acronyms included — is kept as is."""
    hook = (hook or "").strip()
    return hook[:1].upper() + hook[1:] if hook[:1].islower() else hook


def _kicker_size(size: int) -> int:
    return max(8, round(size * KICKER_RATIO))


def _hero_size(size: int) -> int:
    return round(size * HERO_RATIO)


def tracked_width(draw, text: str, font, size: int) -> int:
    """The width of letter-spaced text (the kicker), tracking ``_TRACKING`` of its size."""
    if not text:
        return 0
    track = round(size * _TRACKING)
    return sum(round(draw.textlength(ch, font=font)) for ch in text) + track * (len(text) - 1)


def signature_size(height: int) -> int:
    """The byline's pixel size — small and fixed, never fitted."""
    return max(12, round(height * 0.026))


def _block(draw, parts: HeadlineParts, size: int, width: int):
    """Lay the block out at headline ``size``: ``(font, lines, line_height, block_height)`` or None."""
    font = load_font(size)
    line_height = _line_height(font, size)
    gap = round(size * _GAP_RATIO)
    height = 0
    if parts.kicker:
        k_size = _kicker_size(size)
        if tracked_width(draw, parts.kicker, load_font(k_size), k_size) > width:
            return None
        height += _line_height(load_font(k_size), k_size) + gap
    if parts.hero:
        h_size = _hero_size(size)
        h_font = load_font(h_size)
        if _ink_width(draw, parts.hero, h_font) > width:
            return None
        height += _line_height(h_font, h_size) + gap
    lines: list[str] = []
    if parts.rest:
        wrapped = _wrap(draw, parts.rest.split(), font, width)
        if not wrapped or len(wrapped) > MAX_LINES:
            return None
        lines = wrapped
        height += line_height * len(lines)
    elif height:
        height -= gap
    return font, lines, line_height, height


def fit_cover(size: tuple[int, int], layout: str, parts: HeadlineParts,
              image=None) -> HeadlineFit:
    """Grow the whole cover block to the largest size its box allows, widening the backing if needed.

    The kicker, hero numeral and headline scale together from the headline size, binary-searched
    to the largest that fits the box (so the text fills the box width), with the byline's strip
    reserved at the bottom. When the headline's cap height is still under ``MIN_CAP_FRACTION`` of
    the image height, the panel widens / the band deepens (``GROW_STEPS``). The largest result
    wins; one still under the floor is logged.

    Args:
        size: ``(width, height)``.
        layout: One of ``LAYOUTS``.
        parts: What to typeset.
        image: The render, for ``full_bleed``.

    Returns:
        The fit.
    """
    from PIL import Image, ImageDraw

    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    w, h = size
    floor = round(h * MIN_CAP_FRACTION["portrait" if h > w else "landscape"])
    best: Optional[HeadlineFit] = None
    for grow in range(GROW_STEPS):
        plan = plan_layout(size, layout, image, grow)
        box = plan.text_box
        reserve = (_line_height(load_font(signature_size(h)), signature_size(h))
                   + round(box.height * 0.04)) if parts.signature else 0
        if not parts.kicker:
            reserve += max(10, round(box.height * 0.08))  # the accent rule's strip
        text_box = Box(box.left, box.top, box.right, box.bottom - reserve)
        low, high, found = 8, max(8, text_box.height // 2), None
        while low <= high:
            mid = (low + high) // 2
            laid = _block(draw, parts, mid, text_box.width)
            if laid and laid[3] <= text_box.height:
                found, low = (mid, laid), mid + 1
            else:
                high = mid - 1
        if found is None:
            found = (8, _block(draw, parts, 8, 10 ** 6))
        font_size, (font, lines, line_height, block_height) = found
        fit = HeadlineFit(plan, text_box, font_size, font, tuple(lines), line_height,
                          cap_height(font), floor, block_height)
        if best is None or fit.cap > best.cap:
            best = fit
        if fit.cap >= floor or layout == FULL_BLEED:
            break
    if best.cap < floor:
        log_info("Headline cap height below the thumbnail floor at the widest backing",
                 action_type="image_compose", cap=best.cap, floor=floor, layout=layout)
    return best


def fit_headline(size: tuple[int, int], layout: str, hook: str, image=None) -> HeadlineFit:
    """Fit a bare hook (no kicker, no byline) — ``fit_cover`` for the simple case.

    Args:
        size: ``(width, height)``.
        layout: One of ``LAYOUTS``.
        hook: The headline.
        image: The render, for ``full_bleed``.

    Returns:
        The fit.
    """
    return fit_cover(size, layout, headline_parts(hook), image)


def _ink_width(draw, text: str, font) -> int:
    """The INK width of ``text`` — its glyph bounding box, side bearings included."""
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return right - left


def _wrap(draw, words: list[str], font, max_width: int) -> Optional[list[str]]:
    """Greedy wrap into lines no wider than ``max_width``; None when a single word overflows."""
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if _ink_width(draw, trial, font) <= max_width:
            current = trial
            continue
        if not current:
            return None
        lines.append(current)
        current = word
        if _ink_width(draw, current, font) > max_width:
            return None
    if current:
        lines.append(current)
    return lines


def fit_text(text: str, box: Box, min_line_px: int, max_lines: int = MAX_LINES):
    """The largest font size whose wrapped text fits inside ``box`` in at most ``max_lines``.

    Binary-searches the size; never returns text wider or taller than the box. Sizes below
    ``min_line_px`` are used only when nothing larger fits — legible beats clipped.

    Args:
        text: The headline.
        box: The safe area.
        min_line_px: The legibility floor for a line's height, logged when it cannot be met.
        max_lines: The most lines allowed.

    Returns:
        ``(font, lines, line_height)``.
    """
    from PIL import Image, ImageDraw

    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    words = text.split()

    def attempt(size: int):
        font = load_font(size)
        line_height = _line_height(font, size)
        lines = _wrap(draw, words, font, box.width)
        if lines and len(lines) <= max_lines and line_height * len(lines) <= box.height:
            return font, lines, line_height
        return None

    # Binary search for the largest size that fits (fit is monotonic in size).
    low, high, best = 8, max(8, box.height // 2), None
    while low <= high:
        mid = (low + high) // 2
        fitted = attempt(mid)
        if fitted:
            best, low = fitted, mid + 1
        else:
            high = mid - 1
    if best is None:
        font = load_font(8)
        return font, [text], _line_height(font, 8)
    if best[2] < min_line_px:
        log_warning("Headline set below the legibility floor to fit its box",
                    action_type="image_compose", line_height=best[2], floor=min_line_px)
    return best


def _line_height(font, size: int) -> int:
    """A line's full height — ascent plus descent, so no glyph ever leaves its line box."""
    try:
        ascent, descent = font.getmetrics()
    except AttributeError:  # pragma: no cover - only PIL's bitmap default lacks metrics
        return round(size * _LINE_SPACING)
    return round((ascent + descent) * 1.02)


def line_origins(box: Box, lines: list[str], font, line_height: int) -> list[tuple[int, int]]:
    """Where each line is drawn so its INK starts exactly at the box's left edge.

    The block is centred vertically in ``box``; each line's origin is shifted by its own left side
    bearing, so a glyph like "4" that inks left of its origin never crosses the margin.

    Args:
        box: The text safe area.
        lines: The wrapped lines.
        font: The fitted font.
        line_height: The fitted line height.

    Returns:
        One ``(x, y)`` draw origin per line.
    """
    from PIL import Image, ImageDraw

    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    y = box.top + (box.height - line_height * len(lines)) // 2
    origins = []
    for line in lines:
        ink_left = draw.textbbox((0, 0), line, font=font)[0]
        origins.append((box.left - ink_left, y))
        y += line_height
    return origins


def _hex(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _scrim(image, region: Box, color: tuple[int, int, int], portrait: bool):
    """A charcoal gradient over ``region``, strongest at its centre line, softening outward."""
    from PIL import Image

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    span = region.height if portrait else region.width
    for i in range(span):
        # 0 at the region's edges, full at its centre.
        alpha = round(_SCRIM_ALPHA * (1 - abs(i - span / 2) / (span / 2)) ** 0.5)
        if portrait:
            line = (region.left, region.top + i, region.right, region.top + i + 1)
        else:
            line = (region.left + i, region.top, region.left + i + 1, region.bottom)
        overlay.paste((*color, alpha), line)
    return Image.alpha_composite(image.convert("RGBA"), overlay)


def _draw_tracked(draw, xy: tuple[int, int], text: str, font, size: int, fill) -> None:
    x, y = xy
    track = round(size * _TRACKING)
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += round(draw.textlength(ch, font=font)) + track


def compose_headline(render_path: str, hook: str, layout: Optional[str] = None,
                     brand: Optional[BrandStyle] = None, surface: str = "newsletter",
                     out_path: Optional[str] = None, kicker: Optional[str] = None,
                     signature: Optional[str] = None,
                     canvas: Optional[tuple[int, int]] = None, hero: bool = True) -> str:
    """Typeset the cover — kicker, hero numeral, headline, byline — onto the render.

    Args:
        render_path: The raw render — it carries no text.
        hook: The headline; a grounded number in it becomes the hero numeral.
        layout: One of ``LAYOUTS``; defaults per surface (``DEFAULT_LAYOUT``). A split layout
            builds a fresh ``CANVAS``-sized image and centre-crops the (square) render into its
            scene region; an overlay layout draws on the render itself.
        brand: The exact colors; the reference brand by default.
        surface: ``newsletter`` or ``post_image`` — picks the default layout and the canvas.
        out_path: Where to write; ``<render>_headline.png`` beside the render by default.
        kicker: Stage 1's uppercase topic tag, set above the headline in the accent color.
        signature: The byline (newsletter title or author name), set small at the bottom in
            off-white at half opacity; omitted when empty.
        canvas: The split canvas size; ``CANVAS[surface]`` by default. ``image_graphics`` passes
            its own (a 1920x1080 cover).
        hero: Whether a number in the hook becomes a hero numeral (``headline_parts``).

    Returns:
        The composite's path.
    """
    from PIL import Image, ImageDraw

    brand = brand or BrandStyle()
    layout = layout if layout in LAYOUTS else DEFAULT_LAYOUT.get(surface, SPLIT_LEFT)
    parts = headline_parts(hook, kicker, signature, hero=hero)
    with Image.open(render_path) as opened:
        render = opened.convert("RGBA")
    dark = _hex(brand.neutral_dark)
    if layout in SPLIT_LAYOUTS:
        # The canvas is built at the surface's FINAL size; the render fills only its own region.
        w, h = canvas or CANVAS.get(surface, CANVAS["newsletter"])
        fit = fit_cover((w, h), layout, parts)
        plan = fit.plan
        image = Image.new("RGBA", (w, h), (*dark, 255))
        image.paste(cover_fit(render, plan.scene), (plan.scene.left, plan.scene.top))
    else:
        image = render
        w, h = image.size
        fit = fit_cover((w, h), layout, parts, image)
        plan = fit.plan
    if plan.scrim is not None:
        image = _scrim(image, plan.scrim, dark, portrait=h > w)
    draw = ImageDraw.Draw(image)
    if plan.backing is not None:
        draw.rectangle((plan.backing.left, plan.backing.top, plan.backing.right - 1,
                        plan.backing.bottom - 1), fill=(*dark, 255))
    if plan.scene is not None:
        # A thin dark-gold seam where the type panel meets the photograph.
        seam = plan.backing
        if layout == SPLIT_LEFT:
            line = (seam.right - _SEAM_RULE, 0, seam.right - 1, h - 1)
        elif layout == SPLIT_RIGHT:
            line = (seam.left, 0, seam.left + _SEAM_RULE - 1, h - 1)
        elif layout == SPLIT_TOP:
            line = (0, seam.bottom - _SEAM_RULE, w - 1, seam.bottom - 1)
        else:
            line = (0, seam.top, w - 1, seam.top + _SEAM_RULE - 1)
        draw.rectangle(line, fill=(*_hex(brand.accent), 255))
    box, text_box = plan.text_box, fit.text_box
    gap = round(fit.size * _GAP_RATIO)
    x = text_box.left
    y = text_box.top + (text_box.height - fit.block_height) // 2
    if parts.kicker:
        k_size = _kicker_size(fit.size)
        k_font = load_font(k_size)
        ink_left = draw.textbbox((0, 0), parts.kicker, font=k_font)[0]
        _draw_tracked(draw, (x - ink_left, y), parts.kicker, k_font, k_size,
                      (*_hex(brand.accent), 255))
        y += _line_height(k_font, k_size) + gap
    if parts.hero:
        h_size = _hero_size(fit.size)
        h_font = load_font(h_size)
        ink_left = draw.textbbox((0, 0), parts.hero, font=h_font)[0]
        draw.text((x - ink_left, y), parts.hero, font=h_font, fill=(*_hex(brand.primary), 255))
        y += _line_height(h_font, h_size) + gap
    rest_color = brand.neutral_light if parts.hero else brand.primary
    for line in fit.lines:
        ink_left = draw.textbbox((0, 0), line, font=fit.font)[0]
        draw.text((x - ink_left, y), line, font=fit.font, fill=(*_hex(rest_color), 255))
        y += fit.line_height
    if not parts.kicker:
        rule_top = y + max(2, (box.bottom - y) // 6)
        rule_bottom = min(rule_top + max(3, round(fit.size * 0.06)), text_box.bottom
                          + max(10, round(box.height * 0.08)))
        draw.rectangle((x, rule_top, x + round(box.width * 0.18), rule_bottom),
                       fill=(*_hex(brand.accent), 255))
    if parts.signature:
        sig_size = signature_size(h)
        sig_font = load_font(sig_size)
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        sig_draw = ImageDraw.Draw(overlay)
        sig_y = box.bottom - _line_height(sig_font, sig_size)
        ink_left = sig_draw.textbbox((0, 0), parts.signature, font=sig_font)[0]
        sig_draw.text((x - ink_left, sig_y), parts.signature, font=sig_font,
                      fill=(*_hex(brand.neutral_light), 128))
        image = Image.alpha_composite(image, overlay)
    out_path = out_path or f"{os.path.splitext(render_path)[0]}_headline.png"
    image.convert("RGB").save(out_path, "PNG")
    return out_path
