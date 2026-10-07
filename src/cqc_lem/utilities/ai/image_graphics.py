"""The ONE place a covers' or post images' DATA GRAPHIC is drawn — by code, never by a model.

Issue #2241, archetype round. The owner's verdict on the photo engine was "still generic with just
random people", and the research behind this module agrees (``docs/visual-archetypes-research.md``):
readers skip anonymous stock people as filler, faces do not help business content, and an
AI-looking person costs trust. The image has to carry the post's IDEA or its EVIDENCE. Five of the
seven visual archetypes do that with the piece's own numbers and steps, drawn here with PIL:

- ``stat_card`` — one verified figure, a one-line context and a source line;
- ``highlight_chart`` — 2-6 comparable values, ONE in gold, the rest grey;
- ``receipt`` — "the real cost of X" line items with a circled amount;
- ``before_after`` — a labelled BEFORE / AFTER split of one grounded measure;
- ``checklist`` — 3-5 of the piece's own steps, some deliberately left unchecked.

FACT SAFETY is the point of drawing them in code. Stage 1 returns each candidate figure as
``{label, value, unit, source_sentence}``; ``validate_graphic_facts`` keeps an item only when its
sentence is in the source, its number is in that sentence verbatim (the drawn string IS the
sentence's own substring), and every content word of its label comes from that sentence. Nothing
is derived — no baseline, no sum, no ratio. ``assert_traceable`` re-checks every drawn string
against its recorded sentence immediately before drawing, and refuses (``UngroundedFactError``)
rather than draw a figure it cannot trace; the caller then falls back to the next archetype.

The headline panel — kicker, headline, byline — is ``image_compose``'s, unchanged: the graphic
fills the split layout's scene region instead of a photograph. Nothing here calls a model, so a
code-drawn image costs $0 in render spend; the vision judge still grades the composite.
"""

import math
import os
import random
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Optional

from cqc_lem.utilities.ai.image_compose import (
    DEFAULT_LAYOUT,
    LAYOUTS,
    SPLIT_LAYOUTS,
    BrandStyle,
    _hex,
    _ink_width,
    _line_height,
    _wrap,
    compose_headline,
    fit_cover,
    headline_parts,
    load_font,
    tracked_width,
)

STAT_CARD = "stat_card"
HIGHLIGHT_CHART = "highlight_chart"
RECEIPT = "receipt"
BEFORE_AFTER = "before_after"
CHECKLIST = "checklist"
CODE_DRAWN_ARCHETYPES = (STAT_CARD, HIGHLIGHT_CHART, RECEIPT, BEFORE_AFTER, CHECKLIST)

# A 16:9 cover and a 4:5 feed post, both at the size LinkedIn shows them sharp.
GRAPHIC_CANVAS = {"newsletter": (1920, 1080), "post_image": (1080, 1350)}
# Legibility floors as fractions of the CANVAS WIDTH — what a 400px-wide feed thumbnail scales
# by — so a body string reads at ~9px and the source line at ~6px there, on either surface.
BODY_MIN = 0.024
SOURCE_MIN = 0.016
_SOURCE_MAX = 0.02
_MARGIN = 0.08
MIN_CHART_ITEMS, MAX_CHART_ITEMS = 2, 6
MIN_COST_ITEMS, MAX_COST_ITEMS = 2, 5
MIN_STEPS, MAX_STEPS = 3, 5
_LABEL_MAX_WORDS, _LABEL_MAX_CHARS = 8, 64
_STEP_MAX_WORDS = 8
_SUBJECT_MAX_WORDS = 4
_SOURCE_NAME_MAX = 60
_MIN_SENTENCE_CHARS = 12
FROM_THE_ARTICLE = "From the article"
_MONO_FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
               "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf")
_TIME_WORDS = ("minute", "hour", "day", "week", "month", "year")
# Label words that need not be in the sentence: they carry no claim.
_LABEL_FREE = frozenset({"the", "and", "for", "with", "per", "from", "into", "than", "that",
                         "this", "your", "their", "our", "its", "who", "what", "are", "was",
                         "were", "all", "any", "each", "every", "you"})


class GraphicError(Exception):
    """A code-drawn graphic cannot be drawn; the caller falls back to the next archetype."""


class UngroundedFactError(GraphicError):
    """A string the graphic would draw does not trace to its recorded source sentence."""


class GraphicLayoutError(GraphicError):
    """The data does not fit legibly — the graphic refuses rather than clip or shrink past a floor."""


@dataclass(frozen=True)
class Placement:
    """One drawn string (or frame) and the box it must stay inside — what the tests assert.

    Attributes:
        role: What it is (``hero``, ``label``, ``value``, ``source``, ``step``, ``paper``…).
        text: The string drawn ('' for a frame).
        box: Its ink box ``(left, top, right, bottom)``.
        bounds: The box it must stay inside, in the same frame of reference.
        size: The effective font size in canvas pixels (0 for a frame).
        frame: ``canvas`` or ``paper`` (the receipt's own, pre-rotation coordinates).
    """

    role: str
    text: str
    box: tuple
    bounds: tuple
    size: int = 0
    frame: str = "canvas"


@dataclass(frozen=True)
class GraphicRender:
    """A drawn graphic: where it is, what it drew, and where.

    Attributes:
        path: The finished composite (graphic + headline panel) on disk.
        archetype: Which archetype was drawn.
        facts: Every drawn fact, each with its ``source_sentence`` — the receipt's trace.
        placements: Every drawn string and its bounds.
        canvas: The composite's ``(width, height)``.
    """

    path: str
    archetype: str
    facts: tuple
    placements: tuple
    canvas: tuple = field(default=(0, 0))


# ---------------------------------------------------------------------------------------------
# Fact safety: extraction validation and render-time traceability.
# ---------------------------------------------------------------------------------------------

_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                         " ": " ", " ": " "})
_VALUE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_NUM_END = r"(?![.,]?\d)"
_NUM_START = r"(?<![\d.,])"
_MULT = r"(?:\s?(?:[KkMmBb](?:n)?(?![A-Za-z])|thousand\b|million\b|billion\b))?"
_MULT_VALUES = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "mn": 1e6,
                "b": 1e9, "bn": 1e9, "billion": 1e9}
CURRENCIES = ("$", "€", "£")


def _norm(text: Any) -> str:
    return " ".join(str(text or "").translate(_QUOTES).split())


def _root(token: str) -> str:
    from cqc_lem.utilities.ai.image_concept import _root as concept_root

    return concept_root(token)


def sentence_in_source(sentence: Any, source: str) -> bool:
    """Is ``sentence`` copied from ``source``? Whitespace, quotes and dashes normalised.

    Args:
        sentence: The sentence Stage 1 says a figure came from.
        source: The analysed text.

    Returns:
        Whether it appears in the source verbatim (case-insensitive), and is a real sentence.
    """
    norm = _norm(sentence)
    return len(norm) >= _MIN_SENTENCE_CHARS and norm.lower() in _norm(source).lower()


def verbatim_figure(value: Any, unit: Any, sentence: Any) -> Optional[str]:
    """The figure as drawn, when ``value`` and ``unit`` appear together in ``sentence``; else None.

    The drawn string is built from the sentence's OWN match: a currency keeps its symbol and any
    K/M/B the sentence wrote, a percentage is set as ``N%``, a multiple as ``Nx``, a unit word as
    the sentence spells it. A number that is not in the sentence — or is in it with a different
    unit — is refused, which is the deterministic fabrication guard.

    Args:
        value: The number as written, e.g. ``"38.2"`` or ``"30,000"``.
        unit: ``$``/``€``/``£``, ``%``, ``x``, a unit word such as ``hours``, or ''.
        sentence: The source sentence.

    Returns:
        The display string, or None.
    """
    value = str(value or "").strip()
    unit = str(unit or "").strip()
    sentence = _norm(sentence)
    if not _VALUE.fullmatch(value) or not sentence:
        return None
    v = re.escape(value)
    if unit in CURRENCIES:
        match = re.search(rf"{re.escape(unit)}\s?{v}{_NUM_END}({_MULT})", sentence)
        return f"{unit}{value}{match.group(1).strip()}" if match else None
    if unit == "%":
        match = re.search(rf"{_NUM_START}{v}{_NUM_END}\s?(?:%|percent\b|per cent\b)", sentence,
                          re.IGNORECASE)
        return f"{value}%" if match else None
    if unit.lower() in ("x", "×", "times"):
        match = re.search(rf"{_NUM_START}{v}{_NUM_END}\s?(?:x(?![A-Za-z])|×|times\b)", sentence,
                          re.IGNORECASE)
        return f"{value}x" if match else None
    if unit:
        if not re.fullmatch(r"[A-Za-z][A-Za-z\-]{1,20}", unit):
            return None
        match = re.search(rf"(?<![\d.,$€£]){v}{_NUM_END}({_MULT})\s?-?\s?"
                          rf"({re.escape(_root(unit.lower()))}[a-z]*)", sentence, re.IGNORECASE)
        return f"{value}{match.group(1).strip()} {match.group(2)}" if match else None
    match = re.search(rf"(?<![\d.,$€£]){v}{_NUM_END}(?!\s?(?:%|percent))", sentence,
                      re.IGNORECASE)
    return value if match else None


def label_grounded(label: Any, sentence: Any) -> bool:
    """Does every content word (and number) of ``label`` come from ``sentence``?

    Args:
        label: The label Stage 1 gave a figure or a step.
        sentence: Its source sentence.

    Returns:
        Whether the label is 1-8 words, at most 64 characters, and drifts from nothing the
        sentence says (words matched by root, so "buyers" grounds in "buyer").
    """
    label, lowered = _norm(label), _norm(sentence).lower()
    if not label or len(label) > _LABEL_MAX_CHARS or len(label.split()) > _LABEL_MAX_WORDS:
        return False
    for number in re.findall(r"\d+(?:[.,]\d+)?", label):
        if number not in lowered:
            return False
    from cqc_lem.utilities.ai.image_concept import _STOPWORDS

    for token in re.findall(r"[a-z][a-z'’]*", label.lower()):
        token = token.split("'")[0].split("’")[0]
        if len(token) < 3 or token in _STOPWORDS or token in _LABEL_FREE:
            continue
        if not re.search(rf"\b{re.escape(_root(token))}", lowered):
            return False
    return True


def _amount(display: str) -> Optional[float]:
    match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s?([KkMmBb]n?|thousand|million|billion)?", display)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    return number * _MULT_VALUES.get((match.group(2) or "").lower(), 1.0)


def _unit_kind(unit: str) -> str:
    unit = (unit or "").strip()
    if unit in CURRENCIES:
        return f"currency:{unit}"
    if unit == "%":
        return "percent"
    if unit.lower() in ("x", "×", "times"):
        return "multiple"
    if unit:
        return f"word:{_root(unit.lower())}"
    return "count"


def validate_fact(raw: Any, source: str) -> tuple[Optional[dict], str]:
    """One ``{label, value, unit, source_sentence}`` item checked against the source.

    Args:
        raw: Stage 1's item.
        source: The analysed text.

    Returns:
        ``(fact, '')`` with ``display`` (the drawn string), ``amount`` and ``kind`` added, or
        ``(None, reason)``.
    """
    if not isinstance(raw, dict):
        return None, "not an object"
    sentence = _norm(raw.get("source_sentence"))
    label = _norm(raw.get("label"))
    value = str(raw.get("value") or "").strip()
    unit = str(raw.get("unit") or "").strip()
    if not sentence_in_source(sentence, source):
        return None, f"source sentence not in the article ({sentence[:60]!r})"
    display = verbatim_figure(value, unit, sentence)
    if not display:
        return None, f"{value}{unit} is not in its source sentence"
    if not label_grounded(label, sentence):
        return None, f"label {label!r} is not grounded in its source sentence"
    amount = _amount(display)
    if amount is None:
        return None, "no numeric amount"
    return ({"label": label, "value": value, "unit": unit, "display": display,
             "amount": amount, "kind": _unit_kind(unit), "source_sentence": sentence}, "")


def _validated(items: Any, source: str, rejected: list) -> list[dict]:
    kept: list[dict] = []
    for item in (items if isinstance(items, list) else []):
        fact, reason = validate_fact(item, source)
        if fact is None:
            rejected.append(reason)
        elif fact["label"].lower() not in (k["label"].lower() for k in kept):
            kept.append(fact)
    return kept


def _majority_kind(facts: list[dict], allowed=None) -> list[dict]:
    """The facts sharing the most common unit kind — only like is ever compared with like."""
    facts = [f for f in facts if allowed is None or allowed(f["kind"])]
    if not facts:
        return []
    counts: dict[str, int] = {}
    for fact in facts:
        counts[fact["kind"]] = counts.get(fact["kind"], 0) + 1
    kind = max(counts, key=lambda k: (counts[k], -[f["kind"] for f in facts].index(k)))
    return [f for f in facts if f["kind"] == kind]


def _grounded_phrase(text: Any, source: str, max_words: int) -> str:
    phrase = _norm(text).strip(" .,:;")
    if not phrase or len(phrase.split()) > max_words or not label_grounded(phrase, source):
        return ""
    return phrase


def _is_cost_kind(kind: str) -> bool:
    return kind.startswith("currency:") or (kind.startswith("word:") and
                                            any(kind[5:].startswith(w[:4]) for w in _TIME_WORDS))


def validate_graphic_facts(raw: Any, source: str) -> dict:
    """Stage 1's ``graphic_facts`` reduced to what may be drawn — every item verbatim-checked.

    Args:
        raw: The ``graphic_facts`` object Stage 1 returned (any shape; tolerant).
        source: The analysed text.

    Returns:
        A dict holding only the sections that validated — ``stat``, ``comparison``, ``costs``,
        ``before_after``, ``steps`` — plus ``source_line`` and ``rejected`` (why each refused
        item was refused, for the receipt). Never raises.
    """
    raw = raw if isinstance(raw, dict) else {}
    rejected: list[str] = []
    out: dict[str, Any] = {}
    name = _norm(raw.get("source_name")).strip(" .,:;")
    out["source_line"] = (f"Source: {name}" if name and len(name) <= _SOURCE_NAME_MAX
                          and name in _norm(source) else FROM_THE_ARTICLE)

    stat, reason = validate_fact(raw.get("thesis_stat"), source) if raw.get("thesis_stat") \
        else (None, "")
    if stat:
        out["stat"] = stat
    elif reason:
        rejected.append(f"thesis_stat: {reason}")

    comparison = raw.get("comparison") if isinstance(raw.get("comparison"), dict) else {}
    items = _majority_kind(_validated(comparison.get("items"), source, rejected))[:MAX_CHART_ITEMS]
    if len(items) >= MIN_CHART_ITEMS:
        raw_items = [i for i in comparison.get("items") or [] if isinstance(i, dict)]
        wanted = comparison.get("highlight")
        highlight = None
        if isinstance(wanted, int) and 0 <= wanted < len(raw_items):
            label = _norm(raw_items[wanted].get("label")).lower()
            highlight = next((n for n, f in enumerate(items) if f["label"].lower() == label), None)
        if highlight is None:
            highlight = max(range(len(items)), key=lambda n: items[n]["amount"])
        annotation = _norm(comparison.get("annotation")).strip(" .")
        from cqc_lem.utilities.ai.image_concept import hook_is_faithful

        if annotation and (len(annotation.split()) > _LABEL_MAX_WORDS
                           or not hook_is_faithful(annotation, source)):
            rejected.append(f"annotation {annotation!r} is not grounded")
            annotation = ""
        out["comparison"] = {"measure": _grounded_phrase(comparison.get("measure"), source, 5),
                             "items": items, "highlight": highlight, "annotation": annotation}

    costs = raw.get("costs") if isinstance(raw.get("costs"), dict) else {}
    cost_items = _majority_kind(_validated(costs.get("items"), source, rejected),
                                _is_cost_kind)[:MAX_COST_ITEMS]
    if len(cost_items) >= MIN_COST_ITEMS:
        total, reason = (validate_fact(costs.get("total"), source) if costs.get("total")
                         else (None, ""))
        if total and total["kind"] != cost_items[0]["kind"]:
            total, reason = None, "total is in a different unit"
        if reason:
            rejected.append(f"total: {reason}")
        out["costs"] = {"subject": _grounded_phrase(costs.get("subject"), source,
                                                    _SUBJECT_MAX_WORDS),
                        "items": cost_items, "total": total}

    pair = raw.get("before_after") if isinstance(raw.get("before_after"), dict) else {}
    if pair:
        before, why_b = validate_fact(pair.get("before"), source)
        after, why_a = validate_fact(pair.get("after"), source)
        if before and after and before["kind"] == after["kind"]:
            out["before_after"] = {"before": before, "after": after}
        else:
            rejected.append(f"before_after: {why_b or why_a or 'different units'}")

    steps = []
    for item in (raw.get("steps") if isinstance(raw.get("steps"), list) else []):
        text = _norm(item.get("text") if isinstance(item, dict) else "").strip(" .")
        sentence = _norm(item.get("source_sentence") if isinstance(item, dict) else "")
        if (sentence_in_source(sentence, source) and 2 <= len(text.split()) <= _STEP_MAX_WORDS
                and label_grounded(text, sentence)
                and text.lower() not in (s["text"].lower() for s in steps)):
            steps.append({"text": text[:1].upper() + text[1:], "source_sentence": sentence})
        elif text:
            rejected.append(f"step {text[:40]!r} is not grounded")
    if len(steps) >= MIN_STEPS:
        out["steps"] = steps[:MAX_STEPS]
    out["rejected"] = rejected[:12]
    return out


def available_archetypes(graphic: Optional[dict]) -> tuple[str, ...]:
    """The code-drawn archetypes this validated data can carry, in ``CODE_DRAWN_ARCHETYPES`` order.

    Args:
        graphic: ``validate_graphic_facts``'s output, or None.

    Returns:
        The archetypes whose section validated.
    """
    graphic = graphic or {}
    have = {STAT_CARD: bool(graphic.get("stat")), HIGHLIGHT_CHART: bool(graphic.get("comparison")),
            RECEIPT: bool(graphic.get("costs")), BEFORE_AFTER: bool(graphic.get("before_after")),
            CHECKLIST: bool(graphic.get("steps"))}
    return tuple(a for a in CODE_DRAWN_ARCHETYPES if have[a])


def drawn_facts(archetype: str, graphic: dict) -> list[dict]:
    """Every fact (or step) ``archetype`` would draw from ``graphic``.

    Args:
        archetype: One of ``CODE_DRAWN_ARCHETYPES``.
        graphic: The validated graphic data.

    Returns:
        The facts, each carrying its ``source_sentence``.

    Raises:
        GraphicError: The archetype's section is missing.
    """
    graphic = graphic or {}
    if archetype == STAT_CARD and graphic.get("stat"):
        return [graphic["stat"]]
    if archetype == HIGHLIGHT_CHART and graphic.get("comparison"):
        return list(graphic["comparison"]["items"])
    if archetype == RECEIPT and graphic.get("costs"):
        costs = graphic["costs"]
        return list(costs["items"]) + ([costs["total"]] if costs.get("total") else [])
    if archetype == BEFORE_AFTER and graphic.get("before_after"):
        return [graphic["before_after"]["before"], graphic["before_after"]["after"]]
    if archetype == CHECKLIST and graphic.get("steps"):
        return list(graphic["steps"])
    raise GraphicError(f"no validated data for {archetype}")


def assert_traceable(archetype: str, graphic: dict) -> list[dict]:
    """Re-check every string ``archetype`` will draw against its recorded source sentence.

    The last gate before ink: a fact whose figure is no longer the sentence's own (a tampered or
    hand-built spec) is refused here, whatever validated it earlier.

    Args:
        archetype: One of ``CODE_DRAWN_ARCHETYPES``.
        graphic: The validated graphic data.

    Returns:
        The drawn facts.

    Raises:
        UngroundedFactError: A figure or label does not trace to its sentence.
        GraphicError: The archetype's section is missing.
    """
    facts = drawn_facts(archetype, graphic)
    for fact in facts:
        sentence = fact.get("source_sentence") or ""
        if "text" in fact and "display" not in fact:
            if not label_grounded(fact["text"], sentence):
                raise UngroundedFactError(f"step {fact['text']!r} does not trace to its sentence")
            continue
        if verbatim_figure(fact.get("value"), fact.get("unit"), sentence) != fact.get("display"):
            raise UngroundedFactError(f"{fact.get('display')!r} does not trace to its sentence")
        if not label_grounded(fact.get("label"), sentence):
            raise UngroundedFactError(f"label {fact.get('label')!r} does not trace to its sentence")
    return facts


# ---------------------------------------------------------------------------------------------
# Drawing.
# ---------------------------------------------------------------------------------------------

def _mix(a: tuple, b: tuple, t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _blend(a: str, b: str, t: float) -> tuple[int, int, int]:
    return _mix(_hex(a), _hex(b), t)


@dataclass(frozen=True)
class _Palette:
    ground: tuple
    soft: tuple
    grey_bar: tuple
    grey_text: tuple
    light: tuple
    gold: tuple
    accent: tuple
    dark: tuple


def _palette(brand: BrandStyle) -> _Palette:
    return _Palette(ground=_hex(brand.neutral_dark),
                    soft=_blend(brand.neutral_dark, brand.neutral_light, 0.07),
                    grey_bar=_blend(brand.neutral_dark, brand.neutral_light, 0.32),
                    grey_text=_blend(brand.neutral_dark, brand.neutral_light, 0.62),
                    light=_hex(brand.neutral_light), gold=_hex(brand.primary),
                    accent=_hex(brand.accent), dark=_hex(brand.neutral_dark))


def _mono_font(size: int):
    from PIL import ImageFont

    for path in _MONO_FONTS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return load_font(size)


class _Canvas:
    """A region being drawn, recording every placement against its bounds."""

    def __init__(self, size: tuple[int, int], color: tuple, basis: int, frame: str = "canvas",
                 offset: tuple[int, int] = (0, 0)):
        from PIL import Image, ImageDraw

        self.image = Image.new("RGBA", size, (*color, 255))
        self.draw = ImageDraw.Draw(self.image)
        self.w, self.h = size
        self.basis = basis
        self.frame = frame
        self.offset = offset
        self.placements: list[Placement] = []

    def floor(self, fraction: float) -> int:
        return max(10, round(self.basis * fraction))

    def text(self, xy: tuple[int, int], text: str, font, size: int, fill: tuple, bounds: tuple,
             role: str, tracked: bool = False) -> tuple:
        x, y = xy
        ink_left = self.draw.textbbox((0, 0), text, font=font)[0]
        if tracked:
            track = round(size * 0.12)
            cx = x
            for ch in text:
                self.draw.text((cx, y), ch, font=font, fill=(*fill, 255))
                cx += round(self.draw.textlength(ch, font=font)) + track
            top, bottom = self.draw.textbbox((x, y), text, font=font)[1::2]
            box = (x, top, cx - track, bottom)
        else:
            self.draw.text((x - ink_left, y), text, font=font, fill=(*fill, 255))
            box = self.draw.textbbox((x - ink_left, y), text, font=font)
        self.record(role, text, box, bounds, size)
        return box

    def record(self, role: str, text: str, box: tuple, bounds: tuple, size: int = 0) -> None:
        ox, oy = self.offset if self.frame == "canvas" else (0, 0)
        shift = (ox, oy, ox, oy)
        self.placements.append(Placement(
            role=role, text=text, box=tuple(int(a + b) for a, b in zip(box, shift)),
            bounds=tuple(int(a + b) for a, b in zip(bounds, shift)), size=size,
            frame=self.frame))


def _fit_block(draw, text: str, width: int, height: int, max_lines: int, high: int, low: int,
               mono: bool = False):
    """The largest size in ``[low, high]`` whose wrap fits ``width``x``height``; None if none."""
    words = text.split()
    best = None
    lo, hi = low, max(low, high)
    while lo <= hi:
        mid = (lo + hi) // 2
        font = _mono_font(mid) if mono else load_font(mid)
        lines = _wrap(draw, words, font, width)
        lh = _line_height(font, mid)
        if lines and len(lines) <= max_lines and lh * len(lines) <= height:
            best, lo = (font, lines, lh, mid), mid + 1
        else:
            hi = mid - 1
    return best


def _safe(c: _Canvas) -> tuple:
    m = round(min(c.w, c.h) * _MARGIN)
    return (m, m, c.w - m, c.h - m)


def _source_line(c: _Canvas, text: str, safe: tuple, pal: _Palette) -> int:
    """Draw the source line at the safe area's bottom; returns its top y."""
    left, _, right, bottom = safe
    fitted = _fit_block(c.draw, text, right - left, c.h, 1, c.floor(_SOURCE_MAX),
                        c.floor(SOURCE_MIN))
    if fitted is None and text != FROM_THE_ARTICLE:
        text = FROM_THE_ARTICLE
        fitted = _fit_block(c.draw, text, right - left, c.h, 1, c.floor(_SOURCE_MAX),
                            c.floor(SOURCE_MIN))
    if fitted is None:
        raise GraphicLayoutError("the source line does not fit")
    font, lines, lh, size = fitted
    top = bottom - lh
    c.text((left, top), lines[0], font, size, pal.grey_text, safe, "source")
    return top


def _lines(c: _Canvas, x: int, y: int, fitted, fill: tuple, bounds: tuple, role: str) -> int:
    font, lines, lh, size = fitted
    for line in lines:
        c.text((x, y), line, font, size, fill, bounds, role)
        y += lh
    return y


def _draw_stat_card(c: _Canvas, graphic: dict, pal: _Palette) -> None:
    stat = graphic["stat"]
    safe = _safe(c)
    left, top, right, _ = safe
    width = right - left
    source_top = _source_line(c, graphic.get("source_line") or FROM_THE_ARTICLE, safe, pal)
    area = source_top - top - c.floor(0.02)
    hero = _fit_block(c.draw, stat["display"], width, round(area * 0.48), 1,
                      round(c.basis * 0.3), c.floor(BODY_MIN) * 2)
    if hero is None:
        raise GraphicLayoutError("the figure does not fit")
    rule_h, gap = max(4, round(hero[3] * 0.045)), round(hero[2] * 0.12)
    label = _fit_block(c.draw, stat["label"], width, area - hero[2] - rule_h - 2 * gap, 3,
                       max(c.floor(BODY_MIN), round(hero[3] * 0.3)), c.floor(BODY_MIN))
    if label is None:
        raise GraphicLayoutError("the context line does not fit")
    block = hero[2] + gap + rule_h + gap + label[2] * len(label[1])
    y = top + max(0, (area - block) // 2)
    y = _lines(c, left, y, hero, pal.gold, safe, "hero") + gap
    c.draw.rectangle((left, y, left + round(width * 0.18), y + rule_h - 1), fill=(*pal.accent, 255))
    _lines(c, left, y + rule_h + gap, label, pal.light, safe, "label")


def _draw_highlight_chart(c: _Canvas, graphic: dict, pal: _Palette) -> None:
    data = graphic["comparison"]
    items, highlight = data["items"], data["highlight"]
    safe = _safe(c)
    left, top, right, _ = safe
    width = right - left
    gap_s = c.floor(0.015)
    bottom = _source_line(c, graphic.get("source_line") or FROM_THE_ARTICLE, safe, pal) - gap_s
    if data.get("annotation"):
        note = _fit_block(c.draw, data["annotation"], width - c.floor(0.03), c.h, 2,
                          c.floor(0.03), c.floor(BODY_MIN))
        if note is None:
            raise GraphicLayoutError("the annotation does not fit")
        note_top = bottom - note[2] * len(note[1])
        mark = max(6, round(note[3] * 0.35))
        c.draw.rectangle((left, note_top + round(note[2] * 0.3), left + mark,
                          note_top + round(note[2] * 0.3) + mark), fill=(*pal.gold, 255))
        _lines(c, left + mark * 2, note_top, note, pal.light, safe, "annotation")
        bottom = note_top - gap_s * 2
    if data.get("measure"):
        measure = data["measure"].upper()
        size = c.floor(0.024)
        while size > c.floor(SOURCE_MIN) and tracked_width(c.draw, measure, load_font(size),
                                                           size) > width:
            size -= 1
        if tracked_width(c.draw, measure, load_font(size), size) <= width:
            box = c.text((left, top), measure, load_font(size), size, pal.grey_text, safe,
                         "measure", tracked=True)
            top = box[3] + gap_s * 2
    values = [f["display"] for f in items]
    chosen = None
    for size in range(c.floor(0.05), c.floor(BODY_MIN) - 1, -1):
        font = load_font(size)
        lh = _line_height(font, size)
        value_w = max(_ink_width(c.draw, v, font) for v in values)
        wrapped = [_wrap(c.draw, f["label"].split(), font, width) for f in items]
        if any(not w or len(w) > 2 for w in wrapped) or value_w >= width * 0.4:
            continue
        bar_h, gap = round(lh * 0.8), round(lh * 0.4)
        heights = [len(w) * lh + round(lh * 0.15) + bar_h for w in wrapped]
        if sum(heights) + gap * (len(items) - 1) <= bottom - top:
            chosen = (font, size, lh, wrapped, bar_h, heights, gap, value_w)
            break
    if chosen is None:
        raise GraphicLayoutError("the chart does not fit")
    font, size, lh, wrapped, bar_h, heights, gap, value_w = chosen
    peak = max(f["amount"] for f in items) or 1.0
    track = width - value_w - round(lh * 0.5)
    y = top + max(0, (bottom - top - sum(heights) - gap * (len(items) - 1)) // 2)
    for n, (fact, lines, row_h) in enumerate(zip(items, wrapped, heights)):
        hot = n == highlight
        ly = y
        for line in lines:
            c.text((left, ly), line, font, size, pal.light if hot else pal.grey_text, safe,
                   "label")
            ly += lh
        bar_top = y + row_h - bar_h
        length = max(round(track * 0.02), round(track * fact["amount"] / peak))
        c.draw.rectangle((left, bar_top, left + length, bar_top + bar_h - 1),
                         fill=(*(pal.gold if hot else pal.grey_bar), 255))
        c.text((left + length + round(lh * 0.35), bar_top + (bar_h - lh) // 2), fact["display"],
               font, size, pal.gold if hot else pal.grey_text, safe, "value")
        y += row_h + gap


def _zigzag(width: int, height: int, tooth: int) -> list[tuple[int, int]]:
    top = [(x, 0 if (x // tooth) % 2 else tooth) for x in range(0, width + 1, tooth)]
    bottom = [(x, height - (0 if (x // tooth) % 2 else tooth)) for x in
              range(width, -1, -tooth)]
    return top + bottom


def _hand_ellipse(draw, box: tuple, color: tuple, width: int, seed: str) -> None:
    """A loose, slightly overshooting marker loop around ``box`` — drawn twice, offset."""
    rng = random.Random(seed)
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    rx, ry = (box[2] - box[0]) / 2, (box[3] - box[1]) / 2
    for _ in range(2):
        start, wobble = rng.uniform(0, 6.28), rng.uniform(0.03, 0.06)
        points = []
        for step in range(0, 75):
            t = start + step / 68 * 2 * math.pi
            r = 1 + wobble * math.sin(3 * t + start)
            points.append((cx + rx * r * math.cos(t), cy + ry * r * math.sin(t)))
        draw.line(points, fill=(*color, 255), width=width, joint="curve")


def _draw_receipt(c: _Canvas, graphic: dict, pal: _Palette) -> None:
    from PIL import Image, ImageDraw, ImageFilter

    costs = graphic["costs"]
    items, total = costs["items"], costs.get("total")
    safe = _safe(c)
    left, top, right, _ = safe
    bottom = _source_line(c, graphic.get("source_line") or FROM_THE_ARTICLE, safe, pal)
    bottom -= c.floor(0.025)
    area_w, area_h = right - left, bottom - top
    angle = -2.5
    sin, cos = math.sin(math.radians(abs(angle))), math.cos(math.radians(abs(angle)))
    pw = round(min(area_w * 0.92, area_h * 1.1))
    ph = round((area_h - pw * sin) / cos * 0.97)
    if pw * cos + ph * sin > area_w:
        pw = round((area_w - ph * sin) / cos)
    paper = _Canvas((pw, ph), pal.light, c.basis, frame="paper")
    pad = round(pw * 0.06)
    tooth = max(8, pw // 40)
    inner = (pad, tooth + round(pad * 0.6), pw - pad, ph - tooth - round(pad * 0.6))
    iw = inner[2] - inner[0]
    subject = costs.get("subject") or ""
    title = f"THE REAL COST OF {subject.upper()}" if subject else "THE REAL COST"
    circled = total or max(items, key=lambda f: f["amount"])
    rows = list(items) + ([total] if total else [])
    floor = c.floor(BODY_MIN)
    chosen = None
    for size in range(c.floor(0.045), floor - 1, -1):
        font = _mono_font(size)
        lh = _line_height(font, size)
        amount_w = max(_ink_width(paper.draw, f["display"], font) for f in rows)
        label_w = iw - amount_w - round(size * 1.2)
        if label_w < iw * 0.45:
            continue
        head = _wrap(paper.draw, title.split(), font, iw)
        wrapped = [_wrap(paper.draw, ("TOTAL" if f is total else f["label"]).split(), font,
                         label_w) for f in rows]
        if not head or len(head) > 2 or any(not w or len(w) > 3 for w in wrapped):
            continue
        rule = round(lh * 0.6)
        needed = (len(head) * lh + rule + sum(len(w) * lh + round(lh * 0.2) for w in wrapped)
                  + rule * (2 if total else 1))
        if needed <= inner[3] - inner[1]:
            chosen = (font, size, lh, head, wrapped, rule, needed)
            break
    if chosen is None:
        raise GraphicLayoutError("the receipt does not fit")
    font, size, lh, head, wrapped, rule, needed = chosen
    y = inner[1] + max(0, (inner[3] - inner[1] - needed) // 2)
    for line in head:
        paper.text((inner[0], y), line, font, size, pal.dark, inner, "title")
        y += lh

    def dashes(at: int) -> None:
        step = max(6, round(size * 0.6))
        for x in range(inner[0], inner[2] - step // 2, step):
            paper.draw.line((x, at, x + step // 2, at), fill=(*pal.grey_bar, 255),
                            width=max(2, size // 12))

    dashes(y + rule // 2)
    y += rule
    circle_box = None
    for fact, lines in zip(rows, wrapped):
        if fact is total:
            dashes(y + rule // 2)
            y += rule
        row_top = y
        for line in lines:
            paper.text((inner[0], y), line, font, size, pal.dark, inner, "label")
            y += lh
        amount_x = inner[2] - _ink_width(paper.draw, fact["display"], font)
        box = paper.text((amount_x, row_top), fact["display"], font, size, pal.dark, inner,
                         "value")
        if fact is circled:
            circle_box = box
        y += round(lh * 0.2)
    if circle_box:
        grow_x, grow_y = round(size * 0.55), round(size * 0.45)
        ring = (circle_box[0] - grow_x, circle_box[1] - grow_y, circle_box[2] + grow_x // 2,
                circle_box[3] + grow_y)
        _hand_ellipse(paper.draw, ring, pal.accent, max(3, size // 7), circled["display"])
    mask = Image.new("L", (pw, ph), 0)
    ImageDraw.Draw(mask).polygon(_zigzag(pw, ph, tooth), fill=255)
    paper.image.putalpha(mask)
    rotated = paper.image.rotate(angle, resample=Image.BICUBIC, expand=True)
    shadow = Image.new("RGBA", rotated.size, (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 140), mask=rotated.split()[3])
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(4, pw // 60)))
    px = left + (area_w - rotated.width) // 2
    py = top + (area_h - rotated.height) // 2
    offset = max(4, pw // 70)
    c.image.alpha_composite(shadow, (px + offset, py + offset))
    c.image.alpha_composite(rotated, (px, py))
    c.record("paper", "", (px, py, px + rotated.width, py + rotated.height),
             (left, top, right, bottom + c.floor(0.025)))
    c.placements.extend(paper.placements)


def _draw_before_after(c: _Canvas, graphic: dict, pal: _Palette) -> None:
    pair = graphic["before_after"]
    safe = _safe(c)
    left, top, right, _ = safe
    bottom = _source_line(c, graphic.get("source_line") or FROM_THE_ARTICLE, safe, pal)
    bottom -= c.floor(0.03)
    side_by_side = c.w >= c.h * 0.8
    gutter = round(min(c.w, c.h) * 0.06)
    if side_by_side:
        half = (right - left - gutter) // 2
        panels = [(left, top, left + half, bottom), (right - half, top, right, bottom)]
    else:
        half = (bottom - top - gutter) // 2
        panels = [(left, top, right, top + half), (left, bottom - half, right, bottom)]
    tones = [(_mix(pal.soft, pal.light, 0.05), pal.grey_text, pal.grey_text, "BEFORE",
              pair["before"]),
             (_mix(pal.ground, pal.gold, 0.10), pal.gold, pal.gold, "AFTER", pair["after"])]
    pad = round(min(c.w, c.h) * 0.04)
    inner_w = min(p[2] - p[0] for p in panels) - 2 * pad
    inner_h = min(p[3] - p[1] for p in panels) - 2 * pad
    figure = None
    for size in range(round(c.basis * 0.12), c.floor(BODY_MIN) * 2 - 1, -2):
        font = load_font(size)
        if (all(_ink_width(c.draw, f["display"], font) <= inner_w for f in
                (pair["before"], pair["after"])) and _line_height(font, size) <= inner_h * 0.42):
            figure = (font, size, _line_height(font, size))
            break
    if figure is None:
        raise GraphicLayoutError("the before/after figures do not fit")
    tag_size = c.floor(0.032)
    tag_font = load_font(tag_size)
    tag_h = _line_height(tag_font, tag_size)
    label_room = inner_h - figure[2] - tag_h - 2 * round(pad * 0.5)
    labels = [_fit_block(c.draw, f["label"], inner_w, label_room, 3, c.floor(0.045),
                         c.floor(BODY_MIN)) for f in (pair["before"], pair["after"])]
    if any(fitted is None for fitted in labels):
        raise GraphicLayoutError("a before/after label does not fit")
    size = min(fitted[3] for fitted in labels)
    labels = [_fit_block(c.draw, f["label"], inner_w, label_room, 3, size, size)
              for f in (pair["before"], pair["after"])]
    for (ground, tag_color, figure_color, tag, fact), panel, label in zip(tones, panels, labels):
        c.draw.rectangle((panel[0], panel[1], panel[2] - 1, panel[3] - 1), fill=(*ground, 255))
        bounds = (panel[0] + pad, panel[1] + pad, panel[2] - pad, panel[3] - pad)
        block = tag_h + round(pad * 0.5) + figure[2] + round(pad * 0.5) + label[2] * len(label[1])
        y = bounds[1] + max(0, (bounds[3] - bounds[1] - block) // 2)
        c.text((bounds[0], y), tag, tag_font, tag_size, tag_color, bounds, "tag", tracked=True)
        y += tag_h + round(pad * 0.5)
        c.text((bounds[0], y), fact["display"], figure[0], figure[1], figure_color, bounds,
               "figure")
        y += figure[2] + round(pad * 0.5)
        _lines(c, bounds[0], y, label, pal.light, bounds, "label")
    # A gold arrowhead on the seam, pointing from BEFORE to AFTER.
    k = round(gutter * 0.55)
    if side_by_side:
        cx, cy = (panels[0][2] + panels[1][0]) // 2, (top + bottom) // 2
        arrow = [(cx - k // 2, cy - k), (cx + k // 2 + 2, cy), (cx - k // 2, cy + k)]
    else:
        cx, cy = (left + right) // 2, (panels[0][3] + panels[1][1]) // 2
        arrow = [(cx - k, cy - k // 2), (cx, cy + k // 2 + 2), (cx + k, cy - k // 2)]
    c.draw.polygon(arrow, fill=(*pal.gold, 255))


_CHECK_PATTERN = {3: ("check", "gap", "open"), 4: ("check", "check", "gap", "open"),
                  5: ("check", "check", "gap", "open", "open")}


def checklist_states(count: int) -> tuple[str, ...]:
    """Which rows are ticked, which carries the gold "?" and which stay open — a visible gap.

    Args:
        count: 3-5 rows.

    Returns:
        One of ``check`` / ``gap`` / ``open`` per row; about a third is shown done, never all
        (the knowledge-gap result in ``docs/visual-archetypes-research.md`` §1.3).
    """
    return _CHECK_PATTERN[max(MIN_STEPS, min(MAX_STEPS, count))][:count]


def _draw_checklist(c: _Canvas, graphic: dict, pal: _Palette) -> None:
    steps = graphic["steps"][:MAX_STEPS]
    states = checklist_states(len(steps))
    safe = _safe(c)
    left, top, right, _ = safe
    # The steps are the piece's own advice, whoever it cites for its numbers.
    bottom = _source_line(c, FROM_THE_ARTICLE, safe, pal) - c.floor(0.03)
    chosen = None
    for size in range(c.floor(0.05), c.floor(BODY_MIN) - 1, -1):
        font = load_font(size)
        lh = _line_height(font, size)
        box = round(lh * 0.95)
        text_w = right - left - box - round(lh * 0.6)
        wrapped = [_wrap(c.draw, s["text"].split(), font, text_w) for s in steps]
        if any(not w or len(w) > 2 for w in wrapped):
            continue
        gap = round(lh * 0.55)
        needed = sum(max(len(w) * lh, box) for w in wrapped) + gap * (len(steps) - 1)
        if needed <= bottom - top:
            chosen = (font, size, lh, box, wrapped, gap, needed)
            break
    if chosen is None:
        raise GraphicLayoutError("the checklist does not fit")
    font, size, lh, box, wrapped, gap, needed = chosen
    y = top + (bottom - top - needed) // 2
    stroke = max(3, box // 12)
    for step, lines, state in zip(steps, wrapped, states):
        by = y + (lh - box) // 2
        c.draw.rectangle((left, by, left + box, by + box), outline=(*pal.grey_text, 255),
                         width=stroke)
        c.record("box", "", (left, by, left + box, by + box), safe)
        if state == "check":
            rng = random.Random(step["text"])
            jitter = [rng.uniform(-0.04, 0.04) * box for _ in range(6)]
            points = [(left + box * 0.18 + jitter[0], by + box * 0.52 + jitter[1]),
                      (left + box * 0.42 + jitter[2], by + box * 0.78 + jitter[3]),
                      (left + box * 0.92 + jitter[4], by + box * 0.12 + jitter[5])]
            c.draw.line(points, fill=(*pal.gold, 255), width=max(4, box // 7), joint="curve")
        elif state == "gap":
            q_font = load_font(round(box * 0.8))
            q_box = c.draw.textbbox((0, 0), "?", font=q_font)
            qx = left + (box - (q_box[2] - q_box[0])) // 2 - q_box[0]
            qy = by + (box - (q_box[3] - q_box[1])) // 2 - q_box[1]
            c.draw.text((qx, qy), "?", font=q_font, fill=(*pal.gold, 255))
        text_x = left + box + round(lh * 0.6)
        fill = pal.grey_text if state == "open" else pal.light
        ty = y
        for line in lines:
            c.text((text_x, ty), line, font, size, fill, safe, "step")
            ty += lh
        y += max(len(lines) * lh, box) + gap


_DRAWERS = {STAT_CARD: _draw_stat_card, HIGHLIGHT_CHART: _draw_highlight_chart,
            RECEIPT: _draw_receipt, BEFORE_AFTER: _draw_before_after,
            CHECKLIST: _draw_checklist}


def render_graphic(archetype: str, graphic: dict, *, surface: str, hook: str,
                   kicker: str = "", signature: str = "", brand: Optional[BrandStyle] = None,
                   layout: Optional[str] = None, out_path: Optional[str] = None) -> GraphicRender:
    """Draw one code-drawn archetype beside the typeset headline panel. $0, no model call.

    Args:
        archetype: One of ``CODE_DRAWN_ARCHETYPES``.
        graphic: ``validate_graphic_facts``'s output (as recorded on the concept).
        surface: ``newsletter`` (1920x1080) or ``post_image`` (1080x1350).
        hook: The headline; set in the panel with no hero numeral (the graphic carries it).
        kicker: The uppercase topic tag.
        signature: The byline.
        brand: The exact colors; the reference brand by default.
        layout: A split layout; the surface default when not one.
        out_path: Where to write; a temp file by default.

    Returns:
        The render, its drawn facts and every placement.

    Raises:
        UngroundedFactError: A figure does not trace to its source sentence.
        GraphicLayoutError: The data cannot be set legibly.
        GraphicError: Anything else that stops the graphic (no hook, unknown archetype).
    """
    if archetype not in _DRAWERS:
        raise GraphicError(f"{archetype!r} is not a code-drawn archetype")
    if not (hook or "").strip():
        raise GraphicError("a code-drawn graphic needs a headline")
    facts = assert_traceable(archetype, graphic)
    brand = brand or BrandStyle()
    size = GRAPHIC_CANVAS.get(surface, GRAPHIC_CANVAS["newsletter"])
    layout = layout if layout in SPLIT_LAYOUTS else DEFAULT_LAYOUT.get(surface, LAYOUTS[0])
    if layout not in SPLIT_LAYOUTS:
        layout = SPLIT_LAYOUTS[0]
    plan = fit_cover(size, layout, headline_parts(hook, kicker, signature, hero=False)).plan
    scene = plan.scene
    pal = _palette(brand)
    region = _Canvas((scene.width, scene.height), pal.soft, size[0],
                     offset=(scene.left, scene.top))
    _DRAWERS[archetype](region, graphic, pal)
    handle, region_path = tempfile.mkstemp(suffix=".png", prefix="lem_graphic_")
    os.close(handle)
    try:
        region.image.convert("RGB").save(region_path, "PNG")
        if out_path is None:
            handle, out_path = tempfile.mkstemp(suffix=".png", prefix=f"lem_{archetype}_")
            os.close(handle)
        path = compose_headline(region_path, hook, layout=layout, brand=brand, surface=surface,
                                out_path=out_path, kicker=kicker, signature=signature,
                                canvas=size, hero=False)
    finally:
        try:
            os.remove(region_path)
        except OSError:
            pass
    traced = tuple({k: v for k, v in fact.items() if k not in ("amount", "kind")}
                   for fact in facts)
    return GraphicRender(path=path, archetype=archetype, facts=traced,
                         placements=tuple(region.placements), canvas=size)
