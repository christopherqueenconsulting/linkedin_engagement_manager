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

from cqc_lem.utilities.logger import log_warning

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
PANEL_WIDTH = 0.38
# The centred layouts on a landscape cover keep the headline in the central 60% of the width.
COVER_CENTRAL = 0.6
BAND_HEIGHT = 0.26
_LINE_SPACING = 1.12
_SCRIM_ALPHA = 190

PANEL_LEFT, PANEL_RIGHT = "panel_left", "panel_right"
BAND_TOP, LOWER_THIRD_BAND, FULL_BLEED = "band_top", "lower_third_band", "full_bleed"
LAYOUTS = (PANEL_LEFT, PANEL_RIGHT, BAND_TOP, LOWER_THIRD_BAND, FULL_BLEED)
DEFAULT_LAYOUT = {"newsletter": PANEL_LEFT, "post_image": BAND_TOP}

# User 1's kit is the reference brand; any kit naming its own hexes overrides it.
DEFAULT_PRIMARY = "#E9D437"      # light gold — the headline
DEFAULT_NEUTRAL_DARK = "#1F1F1F"  # charcoal — the panel
DEFAULT_ACCENT = "#A89816"        # dark gold — the rule under the headline


@dataclass(frozen=True)
class BrandStyle:
    """The exact colors a composite uses.

    Attributes:
        primary: Headline color, ``#RRGGBB``.
        neutral_dark: Panel / band / scrim color, ``#RRGGBB``.
        accent: The thin rule under the headline, ``#RRGGBB``.
    """

    primary: str = DEFAULT_PRIMARY
    neutral_dark: str = DEFAULT_NEUTRAL_DARK
    accent: str = DEFAULT_ACCENT


_NAMED_HEX = re.compile(r"([A-Za-z][A-Za-z \-]{1,30}?)\s*\(?\s*(#[0-9A-Fa-f]{6})\b")
_NEUTRAL_NAMES = re.compile(r"charcoal|black|ink|graphite|slate|neutral[_\s-]?dark", re.IGNORECASE)
_PRIMARY_NAMES = re.compile(r"light gold|primary|gold", re.IGNORECASE)


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
    return BrandStyle(primary=primary, neutral_dark=neutral, accent=accent)


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
    """

    layout: str
    backing: Optional[Box]
    scrim: Optional[Box]
    text_box: Box


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


def plan_layout(size: tuple[int, int], layout: str, image=None) -> LayoutPlan:
    """The backing and the text safe area for ``layout`` on a ``size`` frame.

    Every text box keeps at least ``SAFE_MARGIN`` of the frame clear on every side.

    Args:
        size: ``(width, height)``.
        layout: One of ``LAYOUTS``; unknown values take ``panel_left``.
        image: The render, needed only by ``full_bleed`` to find its darkest third.

    Returns:
        The plan.
    """
    w, h = size
    mx, my = round(w * SAFE_MARGIN), round(h * SAFE_MARGIN)
    pad_x = round(w * 0.03)
    if layout == PANEL_RIGHT:
        panel = Box(w - round(w * PANEL_WIDTH), 0, w, h)
        return LayoutPlan(layout, panel, None,
                          Box(panel.left + pad_x, my, w - mx, h - my))
    if layout == BAND_TOP:
        band = Box(0, my, w, my + round(h * BAND_HEIGHT))
        return LayoutPlan(layout, band, None,
                          Box(mx, band.top + round(h * 0.02), w - mx, band.bottom - round(h * 0.02)))
    if layout == LOWER_THIRD_BAND:
        band = Box(0, h - my - round(h * BAND_HEIGHT), w, h - my)
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
    panel = Box(0, 0, round(w * PANEL_WIDTH), h)
    return LayoutPlan(PANEL_LEFT, panel, None, Box(mx, my, panel.right - pad_x, h - my))


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


def compose_headline(render_path: str, hook: str, layout: Optional[str] = None,
                     brand: Optional[BrandStyle] = None, surface: str = "newsletter",
                     out_path: Optional[str] = None) -> str:
    """Typeset ``hook`` onto the render and return the composite's path.

    Args:
        render_path: The raw render — it carries no text.
        hook: The headline, set exactly as given.
        layout: One of ``LAYOUTS``; defaults per surface (``DEFAULT_LAYOUT``).
        brand: The exact colors; the reference brand by default.
        surface: ``newsletter`` or ``post_image`` — sets the legibility floor.
        out_path: Where to write; ``<render>_headline.png`` beside the render by default.

    Returns:
        The composite's path.
    """
    from PIL import Image, ImageDraw

    brand = brand or BrandStyle()
    layout = layout if layout in LAYOUTS else DEFAULT_LAYOUT.get(surface, PANEL_LEFT)
    with Image.open(render_path) as opened:
        image = opened.convert("RGBA")
    w, h = image.size
    plan = plan_layout((w, h), layout, image)
    dark = _hex(brand.neutral_dark)
    if plan.scrim is not None:
        image = _scrim(image, plan.scrim, dark, portrait=h > w)
    draw = ImageDraw.Draw(image)
    if plan.backing is not None:
        draw.rectangle((plan.backing.left, plan.backing.top, plan.backing.right - 1,
                        plan.backing.bottom - 1), fill=(*dark, 255))
    min_line = round(h * MIN_LINE_FRACTION.get(surface, 0.06))
    box = plan.text_box
    # The accent rule gets its own strip under the text, so it can never overlap a descender.
    reserve = max(10, round(box.height * 0.12))
    text_box = Box(box.left, box.top, box.right, box.bottom - reserve)
    font, lines, line_height = fit_text(hook, text_box, min_line)
    origins = line_origins(text_box, lines, font, line_height)
    for (lx, ly), line in zip(origins, lines):
        draw.text((lx, ly), line, font=font, fill=(*_hex(brand.primary), 255))
    rule_top = origins[-1][1] + line_height + max(2, reserve // 4)
    rule_bottom = min(rule_top + max(3, reserve // 5), box.bottom)
    draw.rectangle((box.left, rule_top, box.left + round(box.width * 0.18), rule_bottom),
                   fill=(*_hex(brand.accent), 255))
    out_path = out_path or f"{os.path.splitext(render_path)[0]}_headline.png"
    image.convert("RGB").save(out_path, "PNG")
    return out_path
