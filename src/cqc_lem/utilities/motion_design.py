"""Kinetic motion design for the $0 title card and GIF loops — drawn by code, never by a model.

The owner's verdict on the first live GIF post: it animates, but it is "not that appealing or
dynamic" — mostly static type with a drifting slab and a rule. This module turns the same $0 card
into a kinetic piece. Every style here is PIL frames piped into ffmpeg (no new paid call), in the
brand colours and Montserrat only (``docs/motion-design.md``):

- ``kinetic_mask`` / ``kinetic_slide`` — kinetic typography: the hook's lines exit and mask-reveal
  (or slide) back in, in sequence, a hero number springs in, and ONE key word gets a gold
  underline sweep. Always available, so the rotation always has two options;
- ``stat_counter`` — a VERIFIED figure, at poster scale, springs in and counts up from 0 while a
  gold bar fills in sync, then its label fades in;
- ``chart_draw`` — ``highlight_chart`` bars grow across the frame, the dominant gold bar LAST, then
  the annotation fades in;
- ``checklist_tick`` — the post's own steps appear one by one and their ticks STROKE-draw; the
  last item stays unticked (the curiosity gap, ``image_graphics.checklist_states``);
- ``before_after_wipe`` — a gold divider wipes a huge BEFORE figure into the AFTER figure.

Rules every style obeys:

- **Frame 0 is the COMPLETE piece** — the hook AND the final figure, chart or list — because
  LinkedIn may show frame 0 as the thumbnail. It holds ``MOTION_HOLD_SECONDS``, then the piece
  resets, builds, and holds its complete state again (≥``MOTION_FINAL_HOLD_SECONDS``) to the end.
  A GIF's last frame is sampled at ``t = T``, so it IS frame 0 and the loop has no seam.
- **Ease-out cubic on every motion**, ease-out-back (a small overshoot) on a hero's scale-in.
- **Nothing is ever static:** a soft brand-gold glow drifts round a closed path behind the type,
  one full cycle per piece, so it too ends where it began.
- 24 fps MP4 of 6-8 s; a GIF is ≤12 fps, ≤250 frames, ≤5 MB (``animated_loop`` limits and
  step-down).
- **Every animated number is traceable.** A data style is offered only when
  ``image_graphics.assert_traceable`` passes for its facts, and a counter's last value is the
  fact's own ``display`` string; anything else falls back to kinetic typography.
- **Round 6's caption-band rule holds for a video.** In an MP4 nothing is drawn below the band's
  worst-case top; a GIF has no caption and uses the whole frame. The byline sits in the top row.

The style rotates per author from the title card's history file (``video_title_card``), never the
same style twice in a row.
"""

import math
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from cqc_lem.utilities.logger import log_debug, log_info, log_warning

TASK_NAME = "motion_design"

STYLE_KINETIC_MASK = "kinetic_mask"
STYLE_KINETIC_SLIDE = "kinetic_slide"
STYLE_STAT_COUNTER = "stat_counter"
STYLE_CHART_DRAW = "chart_draw"
STYLE_CHECKLIST_TICK = "checklist_tick"
STYLE_BEFORE_AFTER_WIPE = "before_after_wipe"
KINETIC_STYLES = (STYLE_KINETIC_MASK, STYLE_KINETIC_SLIDE)
DATA_STYLES = (STYLE_STAT_COUNTER, STYLE_CHART_DRAW, STYLE_CHECKLIST_TICK, STYLE_BEFORE_AFTER_WIPE)
# Data styles first: a verified figure on screen beats type alone whenever one is available.
MOTION_STYLES = DATA_STYLES + KINETIC_STYLES

MODE_MP4, MODE_GIF = "mp4", "gif"
MOTION_HOLD_SECONDS = 0.8
MOTION_FINAL_HOLD_SECONDS = 1.2
MOTION_GIF_SECONDS = 6.0
MOTION_GIF_MAX_FPS = 12
MOTION_GIF_WIDTH = 720
MOTION_SIZES = {"1:1": (1080, 1080), "4:5": (1080, 1350), "9:16": (1080, 1920)}

# The code-drawn archetype each data style animates (``image_graphics``).
STYLE_ARCHETYPE = {STYLE_STAT_COUNTER: "stat_card", STYLE_CHART_DRAW: "highlight_chart",
                   STYLE_CHECKLIST_TICK: "checklist", STYLE_BEFORE_AFTER_WIPE: "before_after"}
ARCHETYPE_STYLE = {v: k for k, v in STYLE_ARCHETYPE.items()}

# Data timeline (seconds): the complete piece holds, fades out (reset), then builds back.
_RESET_SECONDS = 0.35
_BUILD_AT = MOTION_HOLD_SECONDS + _RESET_SECONDS + 0.05
_DONE = 1e6  # a build time past every motion: the complete state
_COUNT_SECONDS = 1.8
_WIPE_AT, _WIPE_SECONDS = _BUILD_AT + 1.2, 1.0
# Kinetic typography timing (seconds).
_LINE_STAGGER, _LINE_EXIT, _LINE_ENTER = 0.14, 0.22, 0.5
_UNDERLINE_RETRACT, _UNDERLINE_DELAY, _UNDERLINE_SWEEP = 0.3, 0.15, 0.55
# No label, step or tick is set smaller than this, whatever the frame size.
_MIN_TEXT_PX = 14
# How much of the room under the top row the headline may take, tried in order per style.
# A list or chart needs rows more than the headline needs its third line.
_HEADLINE_SHARES = {STYLE_STAT_COUNTER: (0.4, 0.3, 0.22), STYLE_BEFORE_AFTER_WIPE: (0.4, 0.3, 0.22),
                    STYLE_CHART_DRAW: (0.3, 0.22, 0.4), STYLE_CHECKLIST_TICK: (0.28, 0.22, 0.4)}


def ease_out_cubic(p: float) -> float:
    """``1 - (1 - p)^3`` on ``p`` clamped to [0, 1] — the easing every motion here uses."""
    p = min(1.0, max(0.0, p))
    return 1 - (1 - p) ** 3


def ease_out_back(p: float) -> float:
    """Ease-out with a ~10% overshoot before settling at 1 — a hero's scale-in only."""
    p = min(1.0, max(0.0, p))
    c1 = 1.70158
    return 1 + (c1 + 1) * (p - 1) ** 3 + c1 * (p - 1) ** 2


def _progress(t: float, start: float, duration: float) -> float:
    return ease_out_cubic((t - start) / duration) if duration > 0 else float(t >= start)


def _mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


# ---------------------------------------------------------------------------------------------
# Facts: what may be animated.
# ---------------------------------------------------------------------------------------------
def graphic_from_post(text: Optional[str]) -> dict:
    """Validated graphic sections the post's OWN paragraphs carry, or ``{}``. Never raises.

    Each paragraph is read by ``image_graphics.slide_graphic`` with the whole post as evidence,
    so a figure is kept only when one post sentence states the number and its label together —
    the carousel slide rule. A comparison chart needs Stage 1's ``graphic_facts``; it is never
    guessed from prose.

    Args:
        text: The post.

    Returns:
        ``stat`` / ``before_after`` / ``steps`` sections, the first of each found.
    """
    from cqc_lem.utilities.ai.image_graphics import slide_graphic

    out: dict = {}
    for block in re.split(r"\n\s*\n", text or ""):
        if not block.strip():
            continue
        try:
            found = slide_graphic(None, block, evidence=text)
        except Exception as e:
            log_debug("A paragraph's figures could not be read for motion", error=str(e),
                      task_name=TASK_NAME)
            continue
        if found:
            for key in ("stat", "before_after", "steps"):
                if found[1].get(key) and key not in out:
                    out[key] = found[1][key]
    return out


def motion_graphic(text: Optional[str], concept: Any = None) -> dict:
    """Stage 1's validated graphic when it carries any section, else the post's own figures."""
    graphic = getattr(concept, "graphic", None)
    if isinstance(graphic, dict):
        from cqc_lem.utilities.ai.image_graphics import available_archetypes

        if available_archetypes(graphic):
            return graphic
    return graphic_from_post(text)


def available_styles(graphic: Optional[dict]) -> tuple:
    """The styles these facts can carry, in ``MOTION_STYLES`` order — kinetic always.

    A data style is offered only when ``assert_traceable`` passes for its archetype: a figure that
    no longer traces to its sentence is never animated.

    Args:
        graphic: Validated graphic data (``image_graphics.validate_graphic_facts`` shape).

    Returns:
        The styles.
    """
    from cqc_lem.utilities.ai.image_graphics import GraphicError, assert_traceable

    usable = []
    for style in DATA_STYLES:
        try:
            assert_traceable(STYLE_ARCHETYPE[style], graphic or {})
        except GraphicError:
            continue
        except Exception as e:
            log_debug("Motion facts unreadable — style skipped", error=str(e), style=style,
                      task_name=TASK_NAME)
            continue
        usable.append(style)
    return tuple(usable) + KINETIC_STYLES


def pick_style(options: Sequence[str], recent: Sequence[str], seed: str = "",
               prefer: Optional[str] = None) -> str:
    """The next piece's style: never the last one, least-recently-used otherwise.

    Args:
        options: ``available_styles`` output (always at least the two kinetic styles).
        recent: This author's recent styles, most recent first.
        seed: Stable per-piece text for the no-history case.
        prefer: A style to take when it is available and did not just run (a GIF of a
            code-drawn still animates the still's own archetype).

    Returns:
        One of ``options``.
    """
    from cqc_lem.utilities.ai.post_treatment import lru_order

    options = [o for o in options if o in MOTION_STYLES] or list(KINETIC_STYLES)
    recent = [r for r in recent if r]
    last = recent[0] if recent else None
    if prefer in options and prefer != last:
        return prefer
    fresh = [o for o in options if o != last] or options
    # A data style that has not run lately goes first; kinetic is the fallback, not the default.
    ordered = lru_order(fresh, recent, seed)
    unused_data = [o for o in ordered if o in DATA_STYLES and o not in recent]
    return (unused_data or ordered)[0]


def recent_motions(user_id: Optional[int]) -> list:
    """This author's recent motion styles, most recent first. Never raises."""
    from cqc_lem.utilities.video_title_card import read_card_history

    return [m for m in read_card_history(user_id).get("motions", []) if m in MOTION_STYLES]


def record_motion(user_id: Optional[int], style: str) -> None:
    """Prepend ``style`` to the author's motion history (the title card's file). Never raises."""
    from cqc_lem.utilities.video_title_card import write_card_history

    write_card_history(user_id, motions=[style, *recent_motions(user_id)])


# ---------------------------------------------------------------------------------------------
# Plans.
# ---------------------------------------------------------------------------------------------
@dataclass(eq=False)
class _Sprite:
    """One kinetic row: its text drawn once on a transparent strip at its rest position."""

    image: Any
    top: int
    hero: bool = False


@dataclass(eq=False)
class MotionPlan:
    """Everything a frame needs, decided once. ``render_motion_frame`` only reads it."""

    style: str
    mode: str
    size: tuple
    seconds: float
    palette: Any
    hook: str
    base: Any                       # RGBA layer: every static element over a transparent ground
    background: Any = None          # RGB canvas larger than the frame; its crop drifts
    drift: int = 0                  # the glow's orbit radius in pixels (0 = a still ground)
    sprites: tuple = ()             # kinetic rows (the hook), at rest
    underline: tuple = ()           # (x0, y0, x1, y1) of the key word's underline
    region: tuple = ()              # the data region
    data: dict = field(default_factory=dict)


def _is_dark(color: tuple) -> bool:
    return sum(color[:3]) < 384


def _inks(pal: Any) -> dict:
    """The data colours from the card palette: brand gold, ground and the two text inks."""
    return {"gold": pal.rule, "strong": pal.hook, "text": pal.byline,
            "bar": _mix(pal.ground, pal.byline, 0.42),
            "track": _mix(pal.ground, pal.byline, 0.16),
            "on_gold": min(pal.ground, pal.hook, key=sum),
            "light": max(pal.ground, pal.hook, pal.byline, key=sum),
            "accent_text": pal.rule if _is_dark(pal.ground) else pal.hook}


def _font(size: int, medium: bool = False) -> Any:
    from cqc_lem.utilities.ai.image_compose import load_font
    from cqc_lem.utilities.video_title_card import _medium_font

    return _medium_font(size) if medium else load_font(size)


def _draw() -> Any:
    from PIL import Image, ImageDraw

    return ImageDraw.Draw(Image.new("RGB", (8, 8)))


def _text_box(font: Any, text: str) -> tuple:
    return _draw().textbbox((0, 0), text, font=font)


def _layer(size: tuple, pal: Any) -> Any:
    """A transparent RGBA layer whose hidden colour is the ground, so text edges never fringe."""
    from PIL import Image

    return Image.new("RGBA", size, (*pal.ground, 0))


def _floor(size: tuple, mode: str) -> int:
    """The lowest y anything may reach: above the caption band in a video, the margin in a GIF."""
    height = size[1]
    if mode == MODE_GIF:
        return round(height * 0.94)
    from cqc_lem.utilities.video_captions import caption_band_top

    return caption_band_top(size) - round(height * 0.03)


def _background(size: tuple, pal: Any, drift: int) -> Any:
    """The ground with a soft brand-gold glow, oversized by ``drift`` so a crop can orbit it."""
    from PIL import Image

    width, height = size
    canvas = Image.new("RGB", (width + 2 * drift, height + 2 * drift), pal.ground)
    if drift <= 0:
        return canvas
    radius = round(width * 0.75)
    falloff = Image.radial_gradient("L").resize((2 * radius, 2 * radius))
    # radial_gradient is 0 at the centre: invert it and square it for a soft, wide shoulder.
    mask = falloff.point(lambda v: round(255 * (1 - min(v, 255) / 255) ** 2))
    strength = 0.13 if _is_dark(pal.ground) else 0.2
    glow = Image.new("RGB", mask.size, _mix(pal.ground, pal.rule, strength))
    canvas.paste(glow, (round(width * 0.55) + drift - radius, round(height * 0.12) + drift - radius),
                 mask)
    return canvas


def _ground(plan: MotionPlan, t: float) -> Any:
    """The moving ground at ``t``: one full orbit per piece, so ``t = T`` is ``t = 0``."""
    width, height = plan.size
    if plan.background is None:
        from PIL import Image

        return Image.new("RGBA", plan.size, (*plan.palette.ground, 255))
    angle = 2 * math.pi * (t / max(0.1, plan.seconds))
    left = plan.drift + round(plan.drift * math.cos(angle))
    top = plan.drift + round(plan.drift * math.sin(angle))
    return plan.background.crop((left, top, left + width, top + height)).convert("RGBA")


def _key_word(words: Sequence) -> Optional[int]:
    """The word the underline sweeps: a figure if the hook has one, else its longest content word."""
    from cqc_lem.utilities.ai.image_concept import _STOPWORDS

    for n, word in enumerate(words):
        if re.search(r"\d", word.text):
            return n
    scored = [(len(re.sub(r"\W", "", w.text)), -n, n) for n, w in enumerate(words)
              if re.sub(r"\W", "", w.text).lower() not in _STOPWORDS
              and len(re.sub(r"\W", "", w.text)) >= 4]
    return max(scored)[2] if scored else None


def _drift_for(size: tuple) -> int:
    return max(4, round(size[0] * 0.05))


def plan_kinetic(layout: Any, style: str, mode: str = MODE_MP4) -> MotionPlan:
    """Kinetic typography over a ``video_title_card`` layout — frame 0 IS the complete card.

    Args:
        layout: ``video_title_card.plan_title_card`` output (``clear_caption=False`` for a GIF).
        style: ``kinetic_mask`` or ``kinetic_slide``.
        mode: ``mp4`` or ``gif``.

    Returns:
        The plan.
    """
    from PIL import ImageDraw

    from cqc_lem.utilities.video_title_card import render_title_card_frame

    width, _height = layout.size
    pal = layout.palette
    base = render_title_card_frame(layout, 0.0, hook=False, backdrop=False)
    pad = round(layout.hook_size * 0.18)
    sprites = []
    if layout.hero and layout.hero_font is not None:
        box = _text_box(layout.hero_font, layout.hero)
        top = layout.hero_xy[1] + box[1] - pad
        strip = _layer((width, box[3] - box[1] + 2 * pad), pal)
        ImageDraw.Draw(strip).text((layout.hero_xy[0], layout.hero_xy[1] - top), layout.hero,
                                   font=layout.hero_font, fill=pal.hook)
        sprites.append(_Sprite(strip, top, hero=True))
    rows: dict = {}
    for word in layout.words:
        rows.setdefault(word.y, []).append(word)
    for y, row in sorted(rows.items()):
        box = _text_box(layout.hook_font, row[0].text)
        top = y - pad
        strip = _layer((width, max(box[3], layout.hook_size) + 2 * pad), pal)
        draw = ImageDraw.Draw(strip)
        for word in row:
            draw.text((word.x, word.y - top), word.text, font=layout.hook_font, fill=pal.hook)
        sprites.append(_Sprite(strip, top))
    underline: tuple = ()
    key = _key_word(layout.words)
    if key is not None:
        word = layout.words[key]
        left, _t, right, bottom = _draw().textbbox((word.x, word.y), word.text,
                                                   font=layout.hook_font)
        thick = max(4, round(layout.hook_size * 0.08))
        gap = max(3, round(layout.hook_size * 0.06))
        underline = (left, bottom + gap, right, bottom + gap + thick)
    seconds = layout.seconds if mode == MODE_MP4 else MOTION_GIF_SECONDS
    drift = _drift_for(layout.size)
    return MotionPlan(style=style, mode=mode, size=layout.size, seconds=seconds, palette=pal,
                      hook=" ".join(w.text for w in layout.words), base=base,
                      background=_background(layout.size, pal, drift), drift=drift,
                      sprites=tuple(sprites), underline=underline)


def _fit_wrap(text: str, width: int, max_h: int, high: int, low: int, max_lines: int,
              medium: bool = False) -> Optional[tuple]:
    """``(font, lines, line_h)`` for the largest size ``text`` wraps at within the box, or None."""
    from cqc_lem.utilities.ai.image_compose import _line_height, _wrap

    draw = _draw()
    for size_px in range(high, max(low, _MIN_TEXT_PX) - 1, -2):
        font = _font(size_px, medium)
        lines = _wrap(draw, text.split(), font, width)
        line_h = _line_height(font, size_px)
        if lines and len(lines) <= max_lines and line_h * len(lines) <= max_h:
            return font, lines, line_h
    return None


def _fit_number(text: str, width: int, max_h: int, high: int, low: int) -> Any:
    """The largest bold font whose INK fits ``width`` x ``max_h``; ValueError if none."""
    for size_px in range(high, low - 1, -4):
        font = _font(size_px)
        box = _text_box(font, text)
        if box[2] - box[0] <= width and box[3] - box[1] <= max_h:
            return font
    raise ValueError(f"{text!r} does not fit the data region")


def _data_base(hook: str, *, size: tuple, palette: Any, kicker: str, byline: str,
               mode: str, share: float = 0.4) -> tuple:
    """The data styles' static layer: the kicker + byline row, the hook at headline scale, a rule.

    Returns ``(base RGBA layer, data region box)``; raises ``ValueError`` when the hook or the
    byline cannot be set legibly or no data region is left.
    """
    from PIL import ImageDraw

    width, height = size
    margin = round(width * 0.07)
    floor_y = _floor(size, mode)
    base = _layer(size, palette)
    draw = ImageDraw.Draw(base)
    row_y = round(height * 0.04)
    small = _font(max(_MIN_TEXT_PX, round(width * 0.032)))
    by_font = _font(max(_MIN_TEXT_PX, round(width * 0.034)), medium=True)
    kicker = (kicker or "").upper().strip()
    byline = (byline or "").strip()
    by_w = draw.textlength(byline, font=by_font) if byline else 0
    kick_w = draw.textlength(kicker, font=small) if kicker else 0
    if kicker and byline and kick_w + by_w + round(width * 0.05) > width - 2 * margin:
        kicker = ""  # the byline is attribution; the kicker is decoration
    if kicker:
        draw.text((margin, row_y), kicker, font=small, fill=palette.kicker)
    if byline:
        if by_w > width - 2 * margin:
            raise ValueError("the byline does not fit the top row")
        draw.text((round(width - margin - by_w), row_y), byline, font=by_font,
                  fill=palette.byline)
    top = row_y + round(by_font.size * 1.3) + round(height * 0.03)
    room = floor_y - top
    fitted = _fit_wrap(hook, width - 2 * margin, round(room * share), round(width * 0.13),
                       round(width * 0.045), 3)
    if fitted is None:
        raise ValueError("the hook does not fit above the data region")
    font, lines, line_h = fitted
    for n, line in enumerate(lines):
        x = margin - draw.textbbox((0, 0), line, font=font)[0]
        draw.text((x, top + n * line_h), line, font=font, fill=palette.hook)
    rule_y = top + line_h * len(lines) + round(line_h * 0.15)
    rule_h = max(5, round(width * 0.01))
    draw.rectangle((margin, rule_y, margin + round(width * 0.16), rule_y + rule_h),
                   fill=palette.rule)
    region = (margin, rule_y + rule_h + round(height * 0.04), width - margin, floor_y)
    if region[3] - region[1] < round(height * 0.2):
        raise ValueError("no room for the data region under the hook")
    return base, region


def _counter_parts(display: str) -> tuple:
    """``(prefix, number, suffix)`` of a drawn figure: ``"$25.99"`` → ``("$", "25.99", "")``."""
    match = re.search(r"\d[\d,]*(?:\.\d+)?", display or "")
    if not match:
        raise ValueError(f"no number in {display!r}")
    return display[:match.start()], match.group(0), display[match.end():]


def counter_text(display: str, p: float) -> str:
    """The counter at eased progress ``p``: 0 at the start, the fact's OWN ``display`` at 1.

    Decimals and thousands separators follow the display, so the count never shows a precision
    the fact does not. At ``p >= 1`` the result is ``display`` itself, character for character.

    Args:
        display: The verified figure as drawn (``image_graphics`` fact ``display``).
        p: Eased progress in [0, 1].

    Returns:
        The counter's text.
    """
    if p >= 1:
        return display
    prefix, number, suffix = _counter_parts(display)
    decimals = len(number.split(".")[1]) if "." in number else 0
    value = float(number.replace(",", "")) * max(0.0, p)
    shown = f"{value:,.{decimals}f}" if "," in number else f"{value:.{decimals}f}"
    return f"{prefix}{shown}{suffix}"


def _plan_stat(graphic: dict, region: tuple, size: tuple) -> dict:
    fact = graphic["stat"]
    _counter_parts(fact["display"])
    rw, rh = region[2] - region[0], region[3] - region[1]
    width, height = size
    label = _fit_wrap(fact["label"], rw, round(rh * 0.3), round(width * 0.068),
                      round(width * 0.04), 2, medium=True)
    if label is None:
        raise ValueError("the stat's label does not fit")
    label_font, label_lines, label_h = label
    bar_h = max(8, round(height * 0.022))
    gap = round(rh * 0.06)
    room = rh - label_h * len(label_lines) - bar_h - 2 * gap
    number_font = _fit_number(fact["display"], rw, min(room, round(height * 0.4)),
                              round(width * 0.5), round(width * 0.08))
    box = _text_box(number_font, fact["display"])
    group = (box[3] - box[1]) + gap + label_h * len(label_lines) + gap + bar_h
    y = region[1] + max(0, (rh - group) // 2)
    number_y = y - box[1]
    label_y = y + (box[3] - box[1]) + gap
    return {"fact": fact, "number_font": number_font, "number_xy": (region[0] - box[0], number_y),
            "number_box": (region[0], y, region[0] + box[2] - box[0], y + box[3] - box[1]),
            "label_font": label_font, "label_lines": label_lines, "label_h": label_h,
            "label_y": label_y, "bar_y": label_y + label_h * len(label_lines) + gap,
            "bar_h": bar_h}


def _plan_chart(graphic: dict, region: tuple, size: tuple) -> dict:
    comparison = graphic["comparison"]
    items = list(comparison["items"])
    highlight = int(comparison.get("highlight") or 0)
    annotation = comparison.get("annotation") or ""
    rw, rh = region[2] - region[0], region[3] - region[1]
    width, height = size
    ann = _fit_wrap(annotation, rw, round(rh * 0.16), round(width * 0.05), round(width * 0.03),
                    2, medium=True) if annotation else None
    if annotation and ann is None:
        raise ValueError("the annotation does not fit")
    ann_h = ann[2] * len(ann[1]) + round(rh * 0.04) if ann else 0
    row_h = (rh - ann_h) // len(items)
    label_px = min(round(width * 0.06), round(row_h * 0.3))
    if label_px < max(_MIN_TEXT_PX, round(width * 0.026)):
        raise ValueError("too many bars for the data region")
    draw = _draw()
    label_font = _font(label_px, medium=True)
    bar_gold = min(round(row_h * 0.5), round(height * 0.11))
    bar_grey = round(bar_gold * 0.68)
    value_font = _font(min(round(width * 0.075), round(bar_grey * 0.9)))
    pad = round(width * 0.025)
    for item in items:
        if draw.textlength(item["label"], font=label_font) > rw:
            raise ValueError("a bar label does not fit")
    # The longest bar spans the whole width with its value set INSIDE its end; a bar too short
    # for that carries its value just past its end.
    top = max(i["amount"] for i in items) or 1.0
    rows = []
    for n, item in enumerate(items):
        y = region[1] + n * row_h
        gold = n == highlight
        thick = bar_gold if gold else bar_grey
        length = max(thick // 2, round(rw * item["amount"] / top))
        value_w = round(draw.textlength(item["display"], font=value_font))
        inside = length + pad + value_w > rw
        if inside and value_w + 2 * pad > length:
            raise ValueError("a value fits neither inside nor beside its bar")
        rows.append({"label": item["label"], "display": item["display"], "y": y,
                     "bar_y": y + round(label_px * 1.35), "thick": thick, "length": length,
                     "gold": gold, "value_x": (region[0] + length - pad - value_w if inside
                                 else region[0] + length + pad),
                     "inside": inside})
    # The non-highlighted bars grow first, in order; the gold bar lands last.
    order = [n for n in range(len(rows)) if not rows[n]["gold"]] + [highlight]
    return {"rows": rows, "order": order, "label_font": label_font, "value_font": value_font,
            "annotation": ann[1] if ann else [], "ann_font": ann[0] if ann else None,
            "ann_h": ann[2] if ann else 0, "ann_y": region[1] + row_h * len(items)
            + round(rh * 0.03)}


def _plan_checklist(graphic: dict, region: tuple, size: tuple) -> dict:
    from cqc_lem.utilities.ai.image_graphics import checklist_states

    steps = [s["text"] for s in graphic["steps"]]
    rh = region[3] - region[1]
    width = size[0]
    row_h = rh // len(steps)
    box = min(round(width * 0.085), round(row_h * 0.55))
    if box < max(_MIN_TEXT_PX, round(width * 0.03)):
        raise ValueError("too many steps for the data region")
    text_x = region[0] + round(box * 1.45)
    high = min(round(width * 0.075), round(row_h * 0.45))
    fitted = None
    for size_px in range(high, max(_MIN_TEXT_PX, round(width * 0.03)) - 1, -2):
        fits = [_fit_wrap(s, region[2] - text_x, row_h, size_px, size_px, 2, True)
                for s in steps]
        if all(fits):
            fitted = fits
            break
    if fitted is None:
        raise ValueError("a step does not fit the data region")
    rows = []
    for n, (step, (font, lines, line_h)) in enumerate(zip(steps, fitted)):
        y = region[1] + n * row_h
        text_h = line_h * len(lines)
        rows.append({"text": step, "lines": lines, "line_h": line_h,
                     "y": y + (row_h - box) // 2, "text_y": y + (row_h - text_h) // 2})
    return {"rows": rows, "states": checklist_states(len(steps)), "box": box,
            "font": fitted[0][0], "text_x": text_x}


def _plan_before_after(graphic: dict, region: tuple, size: tuple, palette: Any) -> dict:
    from PIL import ImageDraw

    pair = graphic["before_after"]
    rw, rh = region[2] - region[0], region[3] - region[1]
    width, height = size
    inks = _inks(palette)
    tag_font = _font(max(_MIN_TEXT_PX, round(width * 0.055)))
    tag_h = round(tag_font.size * 1.35)
    labels = {}
    for key in ("before", "after"):
        fit = _fit_wrap(pair[key]["label"], rw, round(rh * 0.25), round(width * 0.065),
                        round(width * 0.04), 2, medium=True)
        if fit is None:
            raise ValueError("a before/after label does not fit")
        labels[key] = fit
    label_room = max(f[2] * len(f[1]) for f in labels.values())
    gap = round(rh * 0.04)
    room = min(rh - tag_h - label_room - 2 * gap, round(height * 0.42))
    # One size for both, so the wipe swaps the figure and never jumps its scale.
    fonts = [_fit_number(pair[k]["display"], rw, room, round(width * 0.6), round(width * 0.1))
             for k in ("before", "after")]
    number_font = min(fonts, key=lambda f: f.size)
    layers = {}
    for key, tag, ink in (("before", "BEFORE", inks["text"]),
                          ("after", "AFTER", inks["accent_text"])):
        fact = pair[key]
        font, lines, line_h = labels[key]
        box = _text_box(number_font, fact["display"])
        group = tag_h + (box[3] - box[1]) + 2 * gap + line_h * len(lines)
        y = max(0, (rh - group) // 2)
        layer = _layer((rw, rh), palette)
        draw = ImageDraw.Draw(layer)
        draw.text((0, y), tag, font=tag_font, fill=inks["gold"] if key == "after" else ink)
        number_top = y + tag_h + gap
        draw.text((-box[0], number_top - box[1]), fact["display"], font=number_font, fill=ink)
        label_y = number_top + (box[3] - box[1]) + gap
        for n, line in enumerate(lines):
            draw.text((0, label_y + n * line_h), line, font=font, fill=inks["text"])
        layers[key] = layer
    return {"layers": layers, "facts": pair}


def plan_data(style: str, graphic: dict, hook: str, *, size: tuple, palette: Any,
              kicker: str = "", byline: str = "", seconds: float = 7.0,
              mode: str = MODE_MP4) -> MotionPlan:
    """A data style's plan. Raises ``ValueError`` (cannot be set) or ``GraphicError`` (untraceable).

    Args:
        style: One of ``DATA_STYLES``.
        graphic: Validated graphic data.
        hook: The headline the piece sets.
        size: ``(width, height)``.
        palette: ``video_title_card.TitleCardPalette``.
        kicker: The topic tag ('' for none).
        byline: The author's name ('' for none).
        seconds: The MP4 length (a GIF is ``MOTION_GIF_SECONDS``).
        mode: ``mp4`` or ``gif``.

    Returns:
        The plan.
    """
    from cqc_lem.utilities.ai.image_concept import tidy_figures
    from cqc_lem.utilities.ai.image_graphics import assert_traceable

    if style not in DATA_STYLES:
        raise ValueError(f"{style!r} is not a data style")
    # The last gate before ink, exactly as a still's drawer runs it.
    assert_traceable(STYLE_ARCHETYPE[style], graphic or {})
    hook = " ".join(tidy_figures(hook or "").split())
    if not hook:
        raise ValueError("a motion piece needs a hook")
    planners = {STYLE_STAT_COUNTER: _plan_stat, STYLE_CHART_DRAW: _plan_chart,
                STYLE_CHECKLIST_TICK: _plan_checklist,
                STYLE_BEFORE_AFTER_WIPE: lambda g, r, s: _plan_before_after(g, r, s, palette)}
    refusal: Optional[ValueError] = None
    # The headline takes up to 40% of the frame's room; a video's caption band leaves less, so
    # it gives some back to the data before the style is refused.
    for share in _HEADLINE_SHARES.get(style, _HEADLINE_SHARES[STYLE_STAT_COUNTER]):
        try:
            base, region = _data_base(hook, size=size, palette=palette, kicker=kicker,
                                      byline=byline, mode=mode, share=share)
            data = planners[style](graphic, region, size)
            break
        except ValueError as e:
            refusal = e
    else:
        raise refusal or ValueError("no layout")
    drift = _drift_for(size)
    return MotionPlan(style=style, mode=mode, size=size,
                      seconds=seconds if mode == MODE_MP4 else MOTION_GIF_SECONDS,
                      palette=palette, hook=hook, base=base,
                      background=_background(size, palette, drift), drift=drift,
                      region=region, data=data)


# ---------------------------------------------------------------------------------------------
# Frames.
# ---------------------------------------------------------------------------------------------
def _with_alpha(image: Any, alpha: float) -> Any:
    if alpha >= 1:
        return image
    faded = image.copy()
    faded.putalpha(image.getchannel("A").point(lambda a: round(a * max(0.0, alpha))))
    return faded


def _scaled(image: Any, scale: float) -> Any:
    """``image`` scaled about its ink's centre, on a canvas of the same size."""
    from PIL import Image

    bbox = image.getbbox()
    if not bbox or scale == 1.0:
        return image
    glyphs = image.crop(bbox)
    w = max(1, round(glyphs.size[0] * scale))
    h = max(1, round(glyphs.size[1] * scale))
    out = Image.new("RGBA", image.size, (0, 0, 0, 0))
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    out.alpha_composite(glyphs.resize((w, h), Image.LANCZOS),
                        (round(cx - w / 2), round(cy - h / 2)))
    return out


def _data_clock(t: float) -> tuple:
    """``(build_time, alpha)`` of the data layer at ``t``: complete, fading out, then building."""
    if t < MOTION_HOLD_SECONDS:
        return _DONE, 1.0
    if t < _BUILD_AT:
        return _DONE, 1.0 - _progress(t, MOTION_HOLD_SECONDS, _RESET_SECONDS)
    return t - _BUILD_AT, 1.0


# How far a kinetic_slide row travels, as a share of the width. Round 8: gif_141's 6% / 8% read as
# a dim, "barely perceptible" — a slide a reader sees travels a sixth of the frame.
_SLIDE_EXIT = 0.12
_SLIDE_ENTER = 0.16


def _kinetic_row(plan: MotionPlan, sprite: _Sprite, index: int, t: float) -> tuple:
    """``(dx, dy, alpha, scale)`` for one row at ``t``: at rest outside its own window."""
    width = plan.size[0]
    start = MOTION_HOLD_SECONDS + _UNDERLINE_RETRACT * 0.5 + index * _LINE_STAGGER
    exit_p = _progress(t, start, _LINE_EXIT)
    enter_t = (t - start - _LINE_EXIT) / _LINE_ENTER
    if t < start or enter_t >= 1:
        return 0, 0, 1.0, 1.0
    height = sprite.image.size[1]
    if t < start + _LINE_EXIT:
        if sprite.hero:
            return 0, 0, 1 - exit_p, 1.0 - 0.2 * exit_p
        if plan.style == STYLE_KINETIC_MASK:
            return 0, -round(height * exit_p), 1.0, 1.0
        return round(width * _SLIDE_EXIT * exit_p), 0, 1 - exit_p, 1.0
    enter_p = ease_out_cubic(enter_t)
    if sprite.hero:
        return 0, 0, min(1.0, enter_t * 2), 0.6 + 0.4 * ease_out_back(enter_t)
    if plan.style == STYLE_KINETIC_MASK:
        return 0, round(height * (1 - enter_p)), 1.0, 1.0
    return -round(width * _SLIDE_ENTER * (1 - enter_p)), 0, enter_p, 1.0


def _underline_span(plan: MotionPlan, t: float) -> tuple:
    """The drawn share of the underline as ``(from, to)`` in [0, 1] — full at rest."""
    if t < MOTION_HOLD_SECONDS:
        return 0.0, 1.0
    retract = _progress(t, MOTION_HOLD_SECONDS, _UNDERLINE_RETRACT)
    rows = len(plan.sprites)
    start = (MOTION_HOLD_SECONDS + _UNDERLINE_RETRACT * 0.5 + (rows - 1) * _LINE_STAGGER
             + _LINE_EXIT + _LINE_ENTER + _UNDERLINE_DELAY)
    if t < start:
        return retract, 1.0
    return 0.0, _progress(t, start, _UNDERLINE_SWEEP)


def _render_kinetic(plan: MotionPlan, t: float, frame: Any) -> None:
    from PIL import Image, ImageDraw

    for index, sprite in enumerate(plan.sprites):
        dx, dy, alpha, scale = _kinetic_row(plan, sprite, index, t)
        image = _scaled(sprite.image, scale)
        if dx or dy:
            moved = Image.new("RGBA", image.size, (0, 0, 0, 0))
            moved.paste(image, (dx, dy))
            image = moved
        frame.alpha_composite(_with_alpha(image, alpha), (0, sprite.top))
    if plan.underline:
        lo, hi = _underline_span(plan, t)
        if hi > lo:
            x0, y0, x1, y1 = plan.underline
            span = x1 - x0
            ImageDraw.Draw(frame).rectangle((x0 + round(span * lo), y0, x0 + round(span * hi), y1),
                                            fill=plan.palette.rule)


def _render_stat(plan: MotionPlan, tb: float, frame: Any, inks: dict, alpha: float) -> None:
    from PIL import ImageDraw

    d = plan.data
    x0, _y0, x1, _y1 = plan.region
    count = _progress(tb, 0.0, _COUNT_SECONDS)
    pop = 0.7 + 0.3 * ease_out_back(tb / 0.55)
    layer = _layer(plan.size, plan.palette)
    ImageDraw.Draw(layer).text(d["number_xy"], counter_text(d["fact"]["display"], count),
                               font=d["number_font"], fill=(*inks["strong"], 255))
    if pop != 1.0:
        # Scale about the figure's own box, never the whole frame.
        box = d["number_box"]
        pad = round(plan.size[0] * 0.02)
        crop = (max(0, box[0] - pad), max(0, box[1] - pad), min(plan.size[0], box[2] + 4 * pad),
                min(plan.size[1], box[3] + pad))
        piece = _scaled(layer.crop(crop), pop)
        layer = _layer(plan.size, plan.palette)
        layer.alpha_composite(piece, crop[:2])
    frame.alpha_composite(_with_alpha(layer, alpha))
    overlay = _layer(plan.size, plan.palette)
    draw = ImageDraw.Draw(overlay)
    a = round(255 * alpha)
    draw.rectangle((x0, d["bar_y"], x1, d["bar_y"] + d["bar_h"]), fill=(*inks["track"], a))
    filled = round((x1 - x0) * count)
    if filled > 0:
        draw.rectangle((x0, d["bar_y"], x0 + filled, d["bar_y"] + d["bar_h"]),
                       fill=(*inks["gold"], a))
    # Round 8: gif_123's mid-loop frames showed a bare "7" — the label waited for the count to
    # finish. The label stays up through the count, so any frame reads "7 tests passed".
    label = alpha
    if label > 0:
        for n, line in enumerate(d["label_lines"]):
            draw.text((x0, d["label_y"] + n * d["label_h"]), line, font=d["label_font"],
                      fill=(*inks["text"], round(255 * label)))
    frame.alpha_composite(overlay)


def _render_chart(plan: MotionPlan, tb: float, frame: Any, inks: dict, alpha: float) -> None:
    from PIL import ImageDraw

    d = plan.data
    x0 = plan.region[0]
    overlay = _layer(plan.size, plan.palette)
    draw = ImageDraw.Draw(overlay)
    end = 0.0
    for step, n in enumerate(d["order"]):
        row = d["rows"][n]
        gold = row["gold"]
        begin = step * 0.18 + (0.25 if gold else 0.0)
        span = 0.8 if gold else 0.6
        end = begin + span
        p = _progress(tb, begin, span)
        if p <= 0:
            continue
        a = round(255 * alpha * min(1.0, p * 2))
        draw.text((x0, row["y"]), row["label"], font=d["label_font"], fill=(*inks["text"], a))
        length = max(1, round(row["length"] * p))
        draw.rectangle((x0, row["bar_y"], x0 + length, row["bar_y"] + row["thick"]),
                       fill=(*(inks["gold"] if gold else inks["bar"]), round(255 * alpha)))
        value_a = _progress(tb, end - 0.15, 0.3) * alpha
        if value_a > 0:
            vbox = _text_box(d["value_font"], row["display"])
            if row["inside"]:
                bar = inks["gold"] if gold else inks["bar"]
                ink = inks["on_gold"] if sum(bar) > 384 else inks["light"]
            else:
                ink = inks["accent_text"] if gold else inks["text"]
            draw.text((row["value_x"] - vbox[0],
                       row["bar_y"] + (row["thick"] - (vbox[3] - vbox[1])) // 2 - vbox[1]),
                      row["display"], font=d["value_font"], fill=(*ink, round(255 * value_a)))
    if d["annotation"]:
        a = _progress(tb, end + 0.1, 0.5) * alpha
        if a > 0:
            for n, line in enumerate(d["annotation"]):
                draw.text((x0, d["ann_y"] + n * d["ann_h"]), line, font=d["ann_font"],
                          fill=(*inks["accent_text"], round(255 * a)))
    frame.alpha_composite(overlay)


def _checklist_timing(count: int) -> tuple:
    appear = [n * 0.3 for n in range(count)]
    return appear, appear[-1] + 0.45


def _stroke(draw: Any, points: list, p: float, fill: tuple, width: int) -> None:
    """Draw the first ``p`` (0-1) of the polyline ``points`` — a path, never a glyph."""
    lengths = [math.dist(a, b) for a, b in zip(points, points[1:])]
    remaining = sum(lengths) * min(1.0, max(0.0, p))
    path = [points[0]]
    for (a, b), length in zip(zip(points, points[1:]), lengths):
        if remaining <= 0:
            break
        share = min(1.0, remaining / length) if length else 1.0
        path.append((a[0] + (b[0] - a[0]) * share, a[1] + (b[1] - a[1]) * share))
        remaining -= length
    if len(path) > 1:
        draw.line(path, fill=fill, width=width, joint="curve")
        r = width / 2
        for x, y in (path[0], path[-1]):
            draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def _render_checklist(plan: MotionPlan, tb: float, frame: Any, inks: dict, alpha: float) -> None:
    from PIL import ImageDraw

    d = plan.data
    box = d["box"]
    x0 = plan.region[0]
    width = plan.size[0]
    appear, ticks_from = _checklist_timing(len(d["rows"]))
    overlay = _layer(plan.size, plan.palette)
    draw = ImageDraw.Draw(overlay)
    tick_n = 0
    for n, row in enumerate(d["rows"]):
        p = _progress(tb, appear[n], 0.4)
        if p <= 0:
            continue
        a = round(255 * alpha * p)
        dx = -round(width * 0.05 * (1 - p))
        y = row["y"]
        line = max(3, round(box * 0.09))
        draw.rounded_rectangle((x0 + dx, y, x0 + dx + box, y + box), radius=round(box * 0.18),
                               outline=(*inks["gold"], a), width=line)
        for k, text in enumerate(row["lines"]):
            draw.text((d["text_x"] + dx, row["text_y"] + k * row["line_h"]), text,
                      font=d["font"], fill=(*inks["strong"], a))
        state = d["states"][n]
        if state == "open":
            continue
        tick = _progress(tb, ticks_from + tick_n * 0.4, 0.4)
        tick_n += 1
        if tick <= 0:
            continue
        ta = round(255 * alpha * min(1.0, tick * 2))
        stroke = max(3, round(box * 0.14))
        left, top = x0 + dx, y
        if state == "gap":
            # In progress: a drawn dash across the box, half-done — never a "?" glyph.
            _stroke(draw, [(left + box * 0.26, top + box * 0.5), (left + box * 0.74, top + box * 0.5)],
                    tick, (*inks["gold"], ta), stroke)
            continue
        draw.rounded_rectangle((left, top, left + box, top + box), radius=round(box * 0.18),
                               fill=(*inks["gold"], ta))
        _stroke(draw, [(left + box * 0.24, top + box * 0.52), (left + box * 0.43, top + box * 0.71),
                       (left + box * 0.78, top + box * 0.3)], tick, (*inks["on_gold"], ta), stroke)
    frame.alpha_composite(overlay)


def _render_before_after(plan: MotionPlan, tb: float, frame: Any, inks: dict,
                         alpha: float) -> None:
    from PIL import ImageDraw

    d = plan.data
    x0, y0, x1, y1 = plan.region
    shown = _progress(tb, 0.0, 0.35) * alpha
    if shown <= 0:
        return
    at = _WIPE_AT - _BUILD_AT
    wipe = _progress(tb, at, _WIPE_SECONDS)
    edge = round((x1 - x0) * wipe)
    layer = d["layers"]["before"].copy()
    if edge > 0:
        layer.paste(d["layers"]["after"].crop((0, 0, edge, y1 - y0)), (0, 0))
    frame.alpha_composite(_with_alpha(layer, shown), (x0, y0))
    if 0 < wipe < 1:
        bar = max(6, round(plan.size[0] * 0.012))
        ImageDraw.Draw(frame).rectangle((x0 + edge - bar // 2, y0, x0 + edge + bar // 2, y1),
                                        fill=(*inks["gold"], round(255 * alpha)))


_DATA_RENDERERS = {STYLE_STAT_COUNTER: _render_stat, STYLE_CHART_DRAW: _render_chart,
                   STYLE_CHECKLIST_TICK: _render_checklist,
                   STYLE_BEFORE_AFTER_WIPE: _render_before_after}


def render_motion_frame(plan: MotionPlan, t: float) -> Any:
    """One frame at ``t`` seconds, as a PIL RGB image — frame 0 is the complete piece.

    Args:
        plan: A ``plan_kinetic`` / ``plan_data`` plan.
        t: Seconds from the start.

    Returns:
        The frame.
    """
    frame = _ground(plan, t)
    frame.alpha_composite(plan.base)
    if plan.style in KINETIC_STYLES:
        _render_kinetic(plan, t, frame)
    else:
        tb, alpha = _data_clock(t)
        _DATA_RENDERERS[plan.style](plan, tb, frame, _inks(plan.palette), alpha)
    return frame.convert("RGB")


def frame_times(plan: MotionPlan, fps: int) -> list:
    """The sample times of every frame. A GIF samples ``t = T`` too, so its last frame is its first."""
    if plan.mode == MODE_GIF:
        count = round(plan.seconds * fps) + 1
        return [plan.seconds * n / (count - 1) for n in range(count)]
    return [n / fps for n in range(max(1, round(plan.seconds * fps)))]


# ---------------------------------------------------------------------------------------------
# Choosing and building a piece.
# ---------------------------------------------------------------------------------------------
def build_motion(hook: str, *, graphic: Optional[dict], size: tuple, palette: Any,
                 kicker: str = "", byline: str = "", seconds: float = 7.0, mode: str = MODE_MP4,
                 recent: Sequence[str] = (), seed: str = "", prefer: Optional[str] = None,
                 card_layout: Any = None) -> Optional[MotionPlan]:
    """Pick a style off the author's rotation and plan it, falling back to kinetic. Never raises.

    A data style that cannot be set (or whose facts no longer trace) falls to the next option and
    finally to kinetic typography over ``card_layout`` (planned here when not given — a GIF's
    uses the whole frame, having no caption band).

    Args:
        hook: The headline.
        graphic: Validated graphic data, or None.
        size: ``(width, height)``.
        palette: ``video_title_card.TitleCardPalette``.
        kicker: The topic tag.
        byline: The author's name.
        seconds: The MP4 length.
        mode: ``mp4`` or ``gif``.
        recent: The author's recent styles, most recent first.
        seed: Stable per-piece text.
        prefer: A style to take when available and not the last one.
        card_layout: The title card's layout, for kinetic typography.

    Returns:
        The plan, or None when not even kinetic typography can be set.
    """
    from cqc_lem.utilities.ai.image_graphics import GraphicError

    options = list(available_styles(graphic))
    tried: list = []
    while options:
        style = pick_style(options, recent, seed, prefer)
        options.remove(style)
        tried.append(style)
        try:
            if style in DATA_STYLES:
                return plan_data(style, graphic or {}, hook, size=size, palette=palette,
                                 kicker=kicker, byline=byline, seconds=seconds, mode=mode)
            layout = card_layout
            if layout is None or tuple(layout.size) != tuple(size):
                from cqc_lem.utilities.video_title_card import plan_title_card

                layout = plan_title_card(hook, size=size, palette=palette, kicker=kicker,
                                         byline=byline, seconds=seconds,
                                         clear_caption=mode != MODE_GIF)
            return plan_kinetic(layout, style, mode)
        except (ValueError, GraphicError) as e:
            log_debug("Motion style could not be set — trying the next", style=style,
                      reason=str(e), task_name=TASK_NAME)
        except Exception as e:
            log_warning("Motion style raised — trying the next", exc=e, style=style,
                        task_name=TASK_NAME)
    log_info("No motion style could be set", tried=",".join(tried), task_name=TASK_NAME)
    return None


def _gif_command(ffmpeg: str, size: tuple, fps: int, width: int, out_path: str) -> list:
    """Raw RGB frames in, a palette-optimised looping GIF out (``animated_loop``'s filter)."""
    graph = (f"scale={int(width)}:-2:flags=lanczos,split[a][b];[a]palettegen=stats_mode=full[p];"
             f"[b][p]paletteuse=dither=bayer:bayer_scale=4")
    return [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{size[0]}x{size[1]}", "-r", str(int(fps)), "-i", "-",
            "-filter_complex", graph, "-loop", "0", out_path]


def write_motion_gif(plan: MotionPlan, out_path: str, *, fps: int = MOTION_GIF_MAX_FPS,
                     width: Optional[int] = None) -> bool:
    """Encode a GIF-mode plan to ``out_path`` inside every GIF limit. True only for a real file.

    ≤``MOTION_GIF_MAX_FPS``, ≤``GIF_MAX_FRAMES`` and ≤``GIF_MAX_BYTES``, checked on the FILE
    (``animated_loop.gif_within_limits``), stepping width and fps down
    (``animated_loop._attempt_plan``) for at most ``LOOP_MAX_ATTEMPTS`` encodes.

    Args:
        plan: A ``mode="gif"`` plan.
        out_path: The GIF to write.
        fps: Frames per second (capped at ``MOTION_GIF_MAX_FPS``).
        width: The output width; the plan's own by default.

    Returns:
        Whether a GIF inside the limits was written. No partial file is left behind.
    """
    from cqc_lem.utilities import video_title_card
    from cqc_lem.utilities.animated_loop import GIF_MAX_FRAMES, _attempt_plan, gif_within_limits

    if plan.mode != MODE_GIF:
        raise ValueError("write_motion_gif needs a GIF-mode plan")
    ffmpeg = video_title_card._ffmpeg()
    if not ffmpeg:
        log_debug("ffmpeg is not installed — no motion GIF", task_name=TASK_NAME)
        return False
    fps = min(int(fps), MOTION_GIF_MAX_FPS)
    for attempt, (try_width, try_fps) in enumerate(
            _attempt_plan(width or plan.size[0], fps), start=1):
        times = frame_times(plan, try_fps)[:GIF_MAX_FRAMES]
        command = _gif_command(ffmpeg, plan.size, try_fps, try_width, out_path)
        ok = video_title_card.encode_frames(
            command, (render_motion_frame(plan, t).tobytes() for t in times), out_path,
            what="Motion GIF")
        if ok and gif_within_limits(out_path):
            log_debug("Motion GIF encoded", attempt=attempt, width=try_width, fps=try_fps,
                      task_name=TASK_NAME)
            return True
        video_title_card._remove(out_path)
        if not ok:
            return False
        log_debug(f"Motion GIF attempt {attempt} over limits — stepping down",
                  task_name=TASK_NAME)
    log_info("Motion GIF would not fit the GIF limits", task_name=TASK_NAME)
    return False


def create_motion_gif(hook: Optional[str], *, graphic: Optional[dict] = None,
                      user_id: Optional[int] = None, post_id: Optional[int] = None,
                      kicker: str = "", byline: str = "", ratio: str = "1:1",
                      prefer: Optional[str] = None,
                      out_path: Optional[str] = None) -> Optional[str]:
    """A $0 kinetic GIF loop of the hook (and its verified facts). Never raises.

    Args:
        hook: The headline.
        graphic: Validated graphic data, or None (kinetic typography).
        user_id: The author — brand colours and the style rotation.
        post_id: For the seed and log context.
        kicker: The topic tag.
        byline: The author's name.
        ratio: ``1:1``, ``4:5`` or ``9:16`` (anything else renders 1:1).
        prefer: A style to take when available and not the last one.
        out_path: Where to write; a temp file the caller owns by default.

    Returns:
        The GIF path, or None.
    """
    from cqc_lem.utilities.video_title_card import _brand_for, pick_ground, title_card_palette

    try:
        hook = (hook or "").strip()
        if not hook:
            return None
        design = MOTION_SIZES.get(ratio, MOTION_SIZES["1:1"])
        size = (MOTION_GIF_WIDTH, round(design[1] * MOTION_GIF_WIDTH / design[0]))
        seed = f"{post_id}:{hook}"
        palette = title_card_palette(_brand_for(user_id), pick_ground(seed))
        plan = build_motion(hook, graphic=graphic, size=size, palette=palette, kicker=kicker,
                            byline=byline, mode=MODE_GIF, recent=recent_motions(user_id),
                            seed=seed, prefer=prefer)
        if plan is None:
            return None
        out_path = out_path or os.path.join(tempfile.gettempdir(),
                                            f"motion_{uuid.uuid4().hex}.gif")
        if not write_motion_gif(plan, out_path):
            return None
        record_motion(user_id, plan.style)
        log_info("Rendered a kinetic motion GIF", user_id=user_id, post_id=post_id,
                 style=plan.style, task_name=TASK_NAME)
        return out_path
    except Exception as e:
        log_warning("Motion GIF raised — no loop", exc=e, user_id=user_id, post_id=post_id,
                    task_name=TASK_NAME)
        return None


def loop_from_receipt(receipt: Optional[dict], *, user_id: Optional[int],
                      post_id: Optional[int], ratio: str = "4:5") -> Optional[str]:
    """The $0 kinetic loop of a CODE-DRAWN still, animating the still's own verified facts.

    The receipt carries Stage 1's validated graphic and the archetype that rendered, so the loop
    prefers that archetype's motion (a stat card counts up, a chart draws in) and every figure
    is re-traced before it moves. Never raises.

    Args:
        receipt: The still's brief receipt (``media_provenance.read_brief_receipt``).
        user_id: The author.
        post_id: The post.
        ratio: The still's ratio.

    Returns:
        A temp GIF path the caller owns, or None.
    """
    receipt = receipt or {}
    concept = receipt.get("concept") if isinstance(receipt.get("concept"), dict) else {}
    # Round 8: a quote or typeset card's loop sets the words the STILL sets — its quote or its
    # headline — in kinetic type; the concept's graphic is not on that still, so it never moves.
    card = str(receipt.get("archetype_rendered") or "") in ("quote_card", "typeset_card")
    hook = str(receipt.get("hook_text") or (receipt.get("quote") if card else "")
               or concept.get("hook_phrase") or "").strip()
    graphic = (concept.get("graphic") if isinstance(concept.get("graphic"), dict)
               and not card else None)
    return create_motion_gif(hook, graphic=graphic, user_id=user_id, post_id=post_id,
                             kicker=str(concept.get("kicker") or ""), ratio=ratio,
                             prefer=ARCHETYPE_STYLE.get(str(receipt.get("archetype_rendered"))))
