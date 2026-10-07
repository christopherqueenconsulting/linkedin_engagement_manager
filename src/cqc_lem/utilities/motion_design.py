"""Kinetic motion design for the $0 title card and GIF loops — drawn by code, never by a model.

The owner's verdict on the first live GIF post: it animates, but it is "not that appealing or
dynamic" — mostly static type with a drifting slab and a rule. This module turns the same $0 card
into a kinetic piece. Every style here is PIL frames piped into ffmpeg (no new paid call), in the
brand colours and Montserrat only (``docs/motion-design.md``):

- ``kinetic_mask`` / ``kinetic_slide`` — kinetic typography: after the frame-0 hold the hook's
  lines mask-reveal (or slide) back in, in sequence, a hero number scales in, and ONE key word gets
  a gold underline sweep. Always available, so the rotation always has two options;
- ``stat_counter`` — a VERIFIED figure counts up from 0 with easing while a gold bar fills in sync,
  then its label fades in;
- ``chart_draw`` — ``highlight_chart`` bars grow from the baseline, the gold bar lands last, then
  the annotation fades in;
- ``checklist_tick`` — the post's own steps appear one by one and their ticks draw; the last item
  stays unticked (the curiosity gap, ``image_graphics.checklist_states``);
- ``before_after_wipe`` — a gold divider wipes the BEFORE state into the AFTER state.

Rules every style obeys:

- **Frame 0 is the full hook** (LinkedIn's thumbnail, showcase round 5) and is held for
  ``MOTION_HOLD_SECONDS`` before anything moves.
- **Ease-out cubic on every motion**, 24 fps MP4 of 6-8 s; a GIF is ≤12 fps, ≤250 frames, ≤5 MB
  (``animated_loop`` limits and step-down) and **seamless**: it ends in its start state, so its
  last frame IS its first.
- **One hero motion, at most one secondary.** Nothing else moves.
- **Every animated number is traceable.** A data style is offered only when
  ``image_graphics.assert_traceable`` passes for its facts, and a counter's last value is the
  fact's own ``display`` string; anything else falls back to kinetic typography.
- **Round 6's caption-band rule holds.** Nothing is drawn below the band's worst-case top, and the
  byline sits in the top row, never under the caption.

The style rotates per author from the title card's history file (``video_title_card``), never the
same style twice in a row.
"""

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
MOTION_GIF_SECONDS = 6.0
MOTION_GIF_MAX_FPS = 12
MOTION_GIF_WIDTH = 720
# The GIF's way back to its start state: the data fades (or the underline retracts) over
# [T - _OUTRO_START, T - _OUTRO_END], then the start state holds to the last frame.
_OUTRO_START, _OUTRO_END = 1.0, 0.4
MOTION_SIZES = {"1:1": (1080, 1080), "4:5": (1080, 1350), "9:16": (1080, 1920)}

# The code-drawn archetype each data style animates (``image_graphics``).
STYLE_ARCHETYPE = {STYLE_STAT_COUNTER: "stat_card", STYLE_CHART_DRAW: "highlight_chart",
                   STYLE_CHECKLIST_TICK: "checklist", STYLE_BEFORE_AFTER_WIPE: "before_after"}
ARCHETYPE_STYLE = {v: k for k, v in STYLE_ARCHETYPE.items()}

# Kinetic typography timing (seconds).
_LINE_STAGGER, _LINE_EXIT, _LINE_ENTER = 0.14, 0.22, 0.5
# No label, step or tick is set smaller than this, whatever the frame size.
_MIN_TEXT_PX = 14
_UNDERLINE_DELAY, _UNDERLINE_SWEEP = 0.15, 0.55


def ease_out_cubic(p: float) -> float:
    """``1 - (1 - p)^3`` on ``p`` clamped to [0, 1] — the ONE easing every motion here uses."""
    p = min(1.0, max(0.0, p))
    return 1 - (1 - p) ** 3


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
    data = [o for o in fresh if o in DATA_STYLES]
    # A data style that has not run lately goes first; kinetic is the fallback, not the default.
    ordered = lru_order(fresh, recent, seed)
    unused_data = [o for o in ordered if o in data and o not in recent]
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
    base: Any                       # RGBA: every static element, the hook included for data
    sprites: tuple = ()             # kinetic rows (the hook), at rest
    underline: tuple = ()           # (x0, y0, x1, y1) of the key word's underline
    region: tuple = ()              # the data region
    data: dict = field(default_factory=dict)
    layers: dict = field(default_factory=dict)


def _is_dark(color: tuple) -> bool:
    return sum(color[:3]) < 384


def _inks(pal: Any) -> dict:
    """The data colours from the card palette: brand gold, ground and the two text inks."""
    return {"gold": pal.rule, "strong": pal.hook,
            "text": pal.byline, "bar": _mix(pal.ground, pal.byline, 0.38),
            "track": _mix(pal.ground, pal.byline, 0.16),
            "on_gold": min(pal.ground, pal.hook, key=sum),
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


def _floor(size: tuple) -> int:
    """The lowest y anything may reach: above the caption band's worst case (round 6)."""
    from cqc_lem.utilities.video_captions import caption_band_top
    from cqc_lem.utilities.video_title_card import CAPTION_CLEARANCE

    height = size[1]
    return min(round(height * (1 - CAPTION_CLEARANCE)), caption_band_top(size)) - round(height * 0.04)


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


def plan_kinetic(layout: Any, style: str, mode: str = MODE_MP4) -> MotionPlan:
    """Kinetic typography over a ``video_title_card`` layout — its frame 0 IS the static card.

    Args:
        layout: ``video_title_card.plan_title_card`` output.
        style: ``kinetic_mask`` or ``kinetic_slide``.
        mode: ``mp4`` or ``gif`` (a GIF retracts the underline so it ends where it began).

    Returns:
        The plan.
    """
    from PIL import Image, ImageDraw

    from cqc_lem.utilities.video_title_card import render_title_card_frame

    width, _height = layout.size
    base = render_title_card_frame(layout, 0.0, hook=False).convert("RGBA")
    pad = round(layout.hook_size * 0.18)
    sprites = []
    if layout.hero and layout.hero_font is not None:
        box = _text_box(layout.hero_font, layout.hero)
        top = layout.hero_xy[1] + box[1] - pad
        strip = Image.new("RGBA", (width, box[3] - box[1] + 2 * pad), (0, 0, 0, 0))
        ImageDraw.Draw(strip).text((layout.hero_xy[0], layout.hero_xy[1] - top), layout.hero,
                                   font=layout.hero_font, fill=layout.palette.hook)
        sprites.append(_Sprite(strip, top, hero=True))
    rows: dict = {}
    for word in layout.words:
        rows.setdefault(word.y, []).append(word)
    for y, row in sorted(rows.items()):
        box = _text_box(layout.hook_font, row[0].text)
        top = y - pad
        strip_h = max(box[3], layout.hook_size) + 2 * pad
        strip = Image.new("RGBA", (width, strip_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(strip)
        for word in row:
            draw.text((word.x, word.y - top), word.text, font=layout.hook_font,
                      fill=layout.palette.hook)
        sprites.append(_Sprite(strip, top))
    underline: tuple = ()
    key = _key_word(layout.words)
    if key is not None:
        word = layout.words[key]
        left, _t, right, bottom = _draw().textbbox((word.x, word.y), word.text,
                                                   font=layout.hook_font)
        thick = max(4, round(width * 0.009))
        gap = max(3, round(layout.hook_size * 0.06))
        underline = (left, bottom + gap, right, bottom + gap + thick)
    seconds = layout.seconds if mode == MODE_MP4 else MOTION_GIF_SECONDS
    return MotionPlan(style=style, mode=mode, size=layout.size, seconds=seconds,
                      palette=layout.palette, hook=" ".join(w.text for w in layout.words),
                      base=base, sprites=tuple(sprites), underline=underline)


def _data_base(hook: str, *, size: tuple, palette: Any, kicker: str,
               byline: str) -> tuple:
    """The data styles' static frame: the kicker + byline row, the hook, its rule, the slab.

    Returns ``(base RGBA, data region box)``; raises ``ValueError`` when the hook or the byline
    cannot be set legibly or no data region is left.
    """
    from PIL import Image, ImageDraw

    from cqc_lem.utilities.video_title_card import _fit_lines

    width, height = size
    margin = round(width * 0.08)
    floor_y = _floor(size)
    base = Image.new("RGBA", size, (*palette.ground, 255))
    draw = ImageDraw.Draw(base)
    # The slab sits below the safe area, under where the caption band burns: static ground.
    slab_top = min(round(height * 0.86), floor_y + round(height * 0.06))
    draw.rectangle((round(width * 0.46), slab_top, width, height), fill=palette.slab)
    draw.rectangle((round(width * 0.46), slab_top, width,
                    slab_top + max(6, round(width * 0.008)) - 1), fill=palette.rule)
    row_y = round(height * 0.055)
    small = _font(max(18, round(width * 0.03)))
    by_font = _font(max(18, round(width * 0.032)), medium=True)
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
    top = row_y + round(by_font.size * 1.3) + round(height * 0.035)
    fitted = _fit_lines(draw, hook, width - 2 * margin, round(height * 0.24),
                        round(width * 0.072), round(width * 0.042), 3)
    if fitted is None:
        raise ValueError("the hook does not fit above the data region")
    font, _size_px, lines, line_h = fitted
    for n, line in enumerate(lines):
        x = margin - draw.textbbox((0, 0), line, font=font)[0]
        draw.text((x, top + n * line_h), line, font=font, fill=palette.hook)
    rule_y = top + line_h * len(lines) + round(line_h * 0.25)
    rule_h = max(5, round(width * 0.007))
    draw.rectangle((margin, rule_y, margin + round(width * 0.14), rule_y + rule_h),
                   fill=palette.rule)
    region = (margin, rule_y + rule_h + round(height * 0.04), width - margin, floor_y)
    if region[3] - region[1] < round(height * 0.16):
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


def _fit_one_line(text: str, width: int, high: int, low: int, medium: bool = False) -> Any:
    """The largest font at which ``text`` sets on one line within ``width``; ValueError if none."""
    draw = _draw()
    for size_px in range(high, low - 1, -2):
        font = _font(size_px, medium)
        if draw.textlength(text, font=font) <= width:
            return font
    raise ValueError(f"{text[:40]!r} does not fit legibly")


def _plan_stat(graphic: dict, region: tuple, size: tuple) -> dict:
    fact = graphic["stat"]
    _counter_parts(fact["display"])
    rw, rh = region[2] - region[0], region[3] - region[1]
    width = size[0]
    label_font = _fit_one_line(fact["label"], rw, round(width * 0.04), round(width * 0.026), True)
    bar_h = max(8, round(width * 0.016))
    label_h = round(label_font.size * 1.3)
    gap = round(rh * 0.07)
    number_h = rh - label_h - bar_h - 2 * gap
    number_font = None
    for size_px in range(round(width * 0.2), round(width * 0.07) - 1, -4):
        font = _font(size_px)
        box = _text_box(font, fact["display"])
        if box[2] - box[0] <= rw and box[3] <= number_h:
            number_font = font
            break
    if number_font is None:
        raise ValueError("the figure does not fit the data region")
    number_bottom = region[1] + _text_box(number_font, fact["display"])[3]
    return {"fact": fact, "number_font": number_font, "label_font": label_font,
            "label_y": number_bottom + gap, "bar_y": number_bottom + 2 * gap + label_h,
            "bar_h": bar_h}


def _plan_chart(graphic: dict, region: tuple, size: tuple) -> dict:
    comparison = graphic["comparison"]
    items = list(comparison["items"])
    highlight = int(comparison.get("highlight") or 0)
    annotation = comparison.get("annotation") or ""
    rw, rh = region[2] - region[0], region[3] - region[1]
    width = size[0]
    ann_font = _fit_one_line(annotation, rw, round(width * 0.034), round(width * 0.024),
                             True) if annotation else None
    ann_h = round(ann_font.size * 1.5) if ann_font else 0
    row_h = (rh - ann_h) // len(items)
    label_px = min(round(width * 0.032), round(row_h * 0.36))
    if label_px < max(_MIN_TEXT_PX, round(width * 0.022)):
        raise ValueError("too many bars for the data region")
    draw = _draw()
    label_font = _font(label_px, medium=True)
    value_font = _font(label_px)
    value_w = max(draw.textlength(i["display"], font=value_font) for i in items)
    bar_room = rw - value_w - round(width * 0.025)
    for item in items:
        if draw.textlength(item["label"], font=label_font) > rw:
            raise ValueError("a bar label does not fit")
    top = max(i["amount"] for i in items) or 1.0
    bar_h = max(10, round(row_h * 0.34))
    if round(label_px * 1.3) + bar_h > row_h:
        raise ValueError("no room for a bar under its label")
    rows = []
    for n, item in enumerate(items):
        y = region[1] + n * row_h
        rows.append({"label": item["label"], "display": item["display"],
                     "y": y, "bar_y": y + round(label_px * 1.3),
                     "length": max(bar_h, round(bar_room * item["amount"] / top)),
                     "gold": n == highlight})
    # The non-highlighted bars grow first, in order; the gold bar lands last.
    order = [n for n in range(len(rows)) if not rows[n]["gold"]] + [highlight]
    return {"rows": rows, "order": order, "label_font": label_font, "value_font": value_font,
            "bar_h": bar_h, "annotation": annotation, "ann_font": ann_font,
            "ann_y": region[1] + row_h * len(items) + round(row_h * 0.1)}


def _plan_checklist(graphic: dict, region: tuple, size: tuple) -> dict:
    from cqc_lem.utilities.ai.image_graphics import checklist_states

    steps = [s["text"] for s in graphic["steps"]]
    rh = region[3] - region[1]
    width = size[0]
    row_h = rh // len(steps)
    box = min(round(width * 0.05), round(row_h * 0.62))
    if box < max(_MIN_TEXT_PX, round(width * 0.026)):
        raise ValueError("too many steps for the data region")
    text_x = region[0] + round(box * 1.6)
    high = min(round(width * 0.038), round(row_h * 0.55))
    font = None
    for size_px in range(high, round(width * 0.024) - 1, -2):
        candidate = _font(size_px, medium=True)
        if all(_draw().textlength(s, font=candidate) <= region[2] - text_x for s in steps):
            font = candidate
            break
    if font is None:
        raise ValueError("a step does not fit the data region")
    rows = [{"text": s, "y": region[1] + n * row_h + (row_h - box) // 2}
            for n, s in enumerate(steps)]
    return {"rows": rows, "states": checklist_states(len(steps)), "box": box, "font": font,
            "text_x": text_x}


def _plan_before_after(graphic: dict, region: tuple, size: tuple, palette: Any) -> dict:
    from PIL import Image, ImageDraw

    pair = graphic["before_after"]
    rw, rh = region[2] - region[0], region[3] - region[1]
    width = size[0]
    inks = _inks(palette)
    tag_font = _font(max(18, round(width * 0.03)))
    layers = {}
    for key, tag, ink in (("before", "BEFORE", inks["text"]),
                          ("after", "AFTER", inks["accent_text"])):
        fact = pair[key]
        label_font = _fit_one_line(fact["label"], rw, round(width * 0.04), round(width * 0.026),
                                   True)
        tag_h = round(tag_font.size * 1.6)
        label_h = round(label_font.size * 1.35)
        room = rh - tag_h - label_h
        number_font = None
        for size_px in range(round(width * 0.18), round(width * 0.07) - 1, -4):
            font = _font(size_px)
            box = _text_box(font, fact["display"])
            if box[2] - box[0] <= rw and box[3] <= room:
                number_font = font
                break
        if number_font is None:
            raise ValueError("a before/after figure does not fit the data region")
        layer = Image.new("RGBA", (rw, rh), (*palette.ground, 255))
        draw = ImageDraw.Draw(layer)
        draw.text((0, 0), tag, font=tag_font, fill=inks["gold"] if key == "after" else ink)
        draw.text((0, tag_h), fact["display"], font=number_font, fill=ink)
        number_bottom = tag_h + _text_box(number_font, fact["display"])[3]
        draw.text((0, number_bottom + round(rh * 0.04)), fact["label"], font=label_font,
                  fill=inks["text"])
        layers[key] = layer
    return {"layers": layers, "facts": pair}


def plan_data(style: str, graphic: dict, hook: str, *, size: tuple, palette: Any,
              kicker: str = "", byline: str = "", seconds: float = 7.0,
              mode: str = MODE_MP4) -> MotionPlan:
    """A data style's plan. Raises ``ValueError`` (cannot be set) or ``GraphicError`` (untraceable).

    Args:
        style: One of ``DATA_STYLES``.
        graphic: Validated graphic data.
        hook: The words the frame sets on frame 0.
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
    base, region = _data_base(hook, size=size, palette=palette, kicker=kicker, byline=byline)
    if style == STYLE_STAT_COUNTER:
        data = _plan_stat(graphic, region, size)
    elif style == STYLE_CHART_DRAW:
        data = _plan_chart(graphic, region, size)
    elif style == STYLE_CHECKLIST_TICK:
        data = _plan_checklist(graphic, region, size)
    else:
        data = _plan_before_after(graphic, region, size, palette)
    return MotionPlan(style=style, mode=mode, size=size,
                      seconds=seconds if mode == MODE_MP4 else MOTION_GIF_SECONDS,
                      palette=palette, hook=hook, base=base, region=region, data=data)


# ---------------------------------------------------------------------------------------------
# Frames.
# ---------------------------------------------------------------------------------------------
def _outro(plan: MotionPlan, t: float) -> float:
    """1 while the piece is on screen; eases to 0 at the end of a GIF so it loops to frame 0."""
    if plan.mode != MODE_GIF:
        return 1.0
    start = plan.seconds - _OUTRO_START
    return 1.0 - _progress(t, start, _OUTRO_START - _OUTRO_END)


def _with_alpha(image: Any, alpha: float) -> Any:
    if alpha >= 1:
        return image
    faded = image.copy()
    faded.putalpha(image.getchannel("A").point(lambda a: round(a * max(0.0, alpha))))
    return faded


def _kinetic_row(plan: MotionPlan, sprite: _Sprite, index: int, t: float) -> tuple:
    """``(dx, dy, alpha, scale)`` for one row at ``t``: at rest outside its own window."""
    width = plan.size[0]
    start = MOTION_HOLD_SECONDS + index * _LINE_STAGGER
    exit_p = _progress(t, start, _LINE_EXIT)
    enter_p = _progress(t, start + _LINE_EXIT, _LINE_ENTER)
    if t < start or enter_p >= 1:
        return 0, 0, 1.0, 1.0
    height = sprite.image.size[1]
    if t < start + _LINE_EXIT:
        if sprite.hero:
            return 0, 0, 1 - exit_p, 1.0
        if plan.style == STYLE_KINETIC_MASK:
            return 0, -round(height * exit_p), 1.0, 1.0
        return round(width * 0.06 * exit_p), 0, 1 - exit_p, 1.0
    if sprite.hero:
        return 0, 0, enter_p, 0.86 + 0.14 * enter_p
    if plan.style == STYLE_KINETIC_MASK:
        return 0, round(height * (1 - enter_p)), 1.0, 1.0
    return -round(width * 0.08 * (1 - enter_p)), 0, enter_p, 1.0


def _underline_span(plan: MotionPlan, t: float) -> tuple:
    """The drawn share of the underline as ``(from, to)`` in [0, 1]."""
    rows = len(plan.sprites)
    start = (MOTION_HOLD_SECONDS + (rows - 1) * _LINE_STAGGER + _LINE_EXIT + _LINE_ENTER
             + _UNDERLINE_DELAY)
    grow = _progress(t, start, _UNDERLINE_SWEEP)
    if plan.mode == MODE_GIF:
        retract = _progress(t, plan.seconds - _OUTRO_START, _OUTRO_START - _OUTRO_END - 0.1)
        return retract, grow
    return 0.0, grow


def _render_kinetic(plan: MotionPlan, t: float, frame: Any) -> None:
    from PIL import Image, ImageDraw

    for index, sprite in enumerate(plan.sprites):
        dx, dy, alpha, scale = _kinetic_row(plan, sprite, index, t)
        image = sprite.image
        if scale != 1.0:
            bbox = image.getbbox()
            if bbox:
                glyphs = image.crop(bbox)
                w = max(1, round(glyphs.size[0] * scale))
                h = max(1, round(glyphs.size[1] * scale))
                glyphs = glyphs.resize((w, h), Image.LANCZOS)
                image = Image.new("RGBA", sprite.image.size, (0, 0, 0, 0))
                cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
                image.paste(glyphs, (round(cx - w / 2), round(cy - h / 2)))
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


def _render_stat(plan: MotionPlan, t: float, draw: Any, inks: dict, alpha: float) -> None:
    d = plan.data
    count = _progress(t, MOTION_HOLD_SECONDS, 1.8)
    if t < MOTION_HOLD_SECONDS:
        return
    x0, y0, x1, _y1 = plan.region
    draw.text((x0, y0), counter_text(d["fact"]["display"], count), font=d["number_font"],
              fill=(*inks["strong"], round(255 * alpha)))
    draw.rectangle((x0, d["bar_y"], x1, d["bar_y"] + d["bar_h"]),
                   fill=(*inks["track"], round(255 * alpha)))
    filled = round((x1 - x0) * count)
    if filled > 0:
        draw.rectangle((x0, d["bar_y"], x0 + filled, d["bar_y"] + d["bar_h"]),
                       fill=(*inks["gold"], round(255 * alpha)))
    label = _progress(t, MOTION_HOLD_SECONDS + 1.8, 0.5) * alpha
    if label > 0:
        draw.text((x0, d["label_y"]), d["fact"]["label"], font=d["label_font"],
                  fill=(*inks["text"], round(255 * label)))


def _render_chart(plan: MotionPlan, t: float, draw: Any, inks: dict, alpha: float) -> None:
    d = plan.data
    x0 = plan.region[0]
    start = MOTION_HOLD_SECONDS
    for step, n in enumerate(d["order"]):
        row = d["rows"][n]
        gold = row["gold"]
        begin = start + step * 0.18 + (0.25 if gold else 0.0)
        p = _progress(t, begin, 0.8 if gold else 0.6)
        if p <= 0:
            continue
        a = round(255 * alpha * min(1.0, p * 2))
        draw.text((x0, row["y"]), row["label"], font=d["label_font"], fill=(*inks["text"], a))
        length = max(1, round(row["length"] * p))
        draw.rectangle((x0, row["bar_y"], x0 + length, row["bar_y"] + d["bar_h"]),
                       fill=(*(inks["gold"] if gold else inks["bar"]), round(255 * alpha)))
        value_a = _progress(t, begin + (0.8 if gold else 0.6) - 0.15, 0.3) * alpha
        if value_a > 0:
            draw.text((x0 + row["length"] + round(plan.size[0] * 0.02),
                       row["bar_y"] + (d["bar_h"] - d["value_font"].size) // 2 - 2),
                      row["display"], font=d["value_font"],
                      fill=(*(inks["accent_text"] if gold else inks["text"]), round(255 * value_a)))
    if d["annotation"]:
        done = start + (len(d["order"]) - 1) * 0.18 + 0.25 + 0.8
        a = _progress(t, done + 0.1, 0.5) * alpha
        if a > 0:
            draw.text((x0, d["ann_y"]), d["annotation"], font=d["ann_font"],
                      fill=(*inks["accent_text"], round(255 * a)))


def _checklist_timing(count: int) -> tuple:
    appear = [MOTION_HOLD_SECONDS + n * 0.3 for n in range(count)]
    ticks_from = appear[-1] + 0.45
    return appear, ticks_from


def _render_checklist(plan: MotionPlan, t: float, draw: Any, inks: dict, alpha: float) -> None:
    d = plan.data
    box = d["box"]
    x0 = plan.region[0]
    width = plan.size[0]
    appear, ticks_from = _checklist_timing(len(d["rows"]))
    tick_n = 0
    for n, row in enumerate(d["rows"]):
        p = _progress(t, appear[n], 0.4)
        if p <= 0:
            continue
        a = round(255 * alpha * p)
        dx = -round(width * 0.04 * (1 - p))
        y = row["y"]
        line = max(3, round(box * 0.09))
        draw.rectangle((x0 + dx, y, x0 + dx + box, y + box), outline=(*inks["gold"], a),
                       width=line)
        draw.text((d["text_x"] + dx, y + (box - d["font"].size) // 2 - 2), row["text"],
                  font=d["font"], fill=(*inks["strong"], a))
        state = d["states"][n]
        if state == "open":
            continue
        tick = _progress(t, ticks_from + tick_n * 0.4, 0.35)
        tick_n += 1
        if tick <= 0:
            continue
        ta = round(255 * alpha * min(1.0, tick * 1.5))
        draw.rectangle((x0 + dx, y, x0 + dx + box, y + box), fill=(*inks["gold"], ta))
        if state == "gap":
            qfont = _font(round(box * 0.8))
            qw = draw.textlength("?", font=qfont)
            draw.text((x0 + dx + (box - qw) / 2, y + box * 0.02), "?", font=qfont,
                      fill=(*inks["on_gold"], round(ta * tick)))
            continue
        # The tick draws as a stroke: down to the elbow, then up to the far corner.
        a_pt = (x0 + dx + box * 0.22, y + box * 0.52)
        b_pt = (x0 + dx + box * 0.42, y + box * 0.72)
        c_pt = (x0 + dx + box * 0.80, y + box * 0.28)
        stroke = max(3, round(box * 0.12))
        first = min(1.0, tick / 0.4)
        draw.line([a_pt, (a_pt[0] + (b_pt[0] - a_pt[0]) * first,
                          a_pt[1] + (b_pt[1] - a_pt[1]) * first)],
                  fill=(*inks["on_gold"], ta), width=stroke)
        if tick > 0.4:
            second = (tick - 0.4) / 0.6
            draw.line([b_pt, (b_pt[0] + (c_pt[0] - b_pt[0]) * second,
                              b_pt[1] + (c_pt[1] - b_pt[1]) * second)],
                      fill=(*inks["on_gold"], ta), width=stroke)


_WIPE_AT, _WIPE_SECONDS = MOTION_HOLD_SECONDS + 1.3, 1.2


def _render_before_after(plan: MotionPlan, t: float, frame: Any, inks: dict,
                         alpha: float) -> None:
    from PIL import ImageDraw

    d = plan.data
    x0, y0, x1, y1 = plan.region
    shown = _progress(t, MOTION_HOLD_SECONDS, 0.4) * alpha
    if shown <= 0:
        return
    wipe = _progress(t, _WIPE_AT, _WIPE_SECONDS)
    edge = round((x1 - x0) * wipe)
    layer = d["layers"]["before"].copy()
    if edge > 0:
        layer.paste(d["layers"]["after"].crop((0, 0, edge, y1 - y0)), (0, 0))
    frame.alpha_composite(_with_alpha(layer, shown), (x0, y0))
    if 0 < wipe < 1:
        bar = max(6, round(plan.size[0] * 0.008))
        ImageDraw.Draw(frame).rectangle((x0 + edge - bar // 2, y0, x0 + edge + bar // 2, y1),
                                        fill=(*inks["gold"], round(255 * alpha)))


def render_motion_frame(plan: MotionPlan, t: float) -> Any:
    """One frame at ``t`` seconds, as a PIL RGB image — frame 0 carries the whole hook.

    Args:
        plan: A ``plan_kinetic`` / ``plan_data`` plan.
        t: Seconds from the start.

    Returns:
        The frame.
    """
    from PIL import Image, ImageDraw

    frame = plan.base.copy()
    if plan.style in KINETIC_STYLES:
        _render_kinetic(plan, t, frame)
        return frame.convert("RGB")
    inks = _inks(plan.palette)
    alpha = _outro(plan, t)
    if plan.style == STYLE_BEFORE_AFTER_WIPE:
        _render_before_after(plan, t, frame, inks, alpha)
        return frame.convert("RGB")
    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    {STYLE_STAT_COUNTER: _render_stat, STYLE_CHART_DRAW: _render_chart,
     STYLE_CHECKLIST_TICK: _render_checklist}[plan.style](plan, t, draw, inks, alpha)
    frame.alpha_composite(overlay)
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
    finally to kinetic typography over ``card_layout`` (planned here when not given).

    Args:
        hook: The words frame 0 sets.
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
                                         byline=byline, seconds=seconds)
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
    graph = (f"scale={int(width)}:-2:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];"
             f"[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")
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
        hook: The words frame 0 sets.
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
    hook = str(receipt.get("hook_text") or concept.get("hook_phrase") or "").strip()
    graphic = concept.get("graphic") if isinstance(concept.get("graphic"), dict) else None
    return create_motion_gif(hook, graphic=graphic, user_id=user_id, post_id=post_id,
                             kicker=str(concept.get("kicker") or ""), ratio=ratio,
                             prefer=ARCHETYPE_STYLE.get(str(receipt.get("archetype_rendered"))))

