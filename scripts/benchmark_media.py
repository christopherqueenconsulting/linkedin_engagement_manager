#!/usr/bin/env python3
"""Media-tier benchmark: `lem-vision` and `lem-image` (issue #2251).

`scripts/benchmark_models.py` (#721) measures the TEXT tiers. The two media tiers had no measurement
at all, so a vision or image swap was decided on a spec sheet. This module is the media half; it is
driven from `benchmark_models.py --tiers lem-vision,lem-image` and shares that harness's posture:
the tier's current CHAMPION always runs beside each candidate, so a verdict is a comparison, and a
recommendation is never a write to `.litellm/config.yaml`.

**lem-vision** - a fixed set of synthetic fixtures drawn with PIL at run time (deterministic: the
same bytes every run, no committed binaries, no customer content). Each fixture carries ground truth
for the three questions the render gate leans on: is there stray TEXT, is a stock CLICHÉ object
present (gears / pipes / server rack), and what EMOTION does a face show. A model is scored on its
agreement with that truth, per field and overall.

**lem-image** - a small set of synthetic briefs, each rendered once per model and judged by the
`lem-vision` champion against the gauntlet rubric from #2241/#2248: specificity, no_cliche,
thumbnail_read, text_accuracy, craft, scroll_stop, brand_fit (each 1-5), with that rubric's floors.

**Spend is capped, and the cap is enforced BEFORE any call.** Every planned call is priced up front
from the pinned LiteLLM cost map (`.litellm/model_prices_snapshot.json`) at a deliberately
conservative ceiling; a plan over `--max-spend-usd` (default `BENCHMARK_MAX_SPEND_USD`, else $2.00)
or naming an UNPRICED model is refused outright - an unpriced model is a plan whose cost nobody can
bound. A running meter stops the run if real usage ever outruns the estimate.

Provider calls go straight to OpenAI through `AttributedOpenAI` (the ONE client), not the proxy: the
proxy routes by tier alias, and a candidate that is not in the config has no alias to be reached by.

PURE logic (fixtures, scoring, gate, spend plan, rendering) over a thin I/O layer (`MediaProvider`)
that the tests mock.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import time
from typing import Callable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

MEDIA_TIERS = ("lem-vision", "lem-image")
DEFAULT_MAX_SPEND_USD = 2.00
DEFAULT_PRICES = ".litellm/model_prices_snapshot.json"
DEFAULT_PROVIDER_SNAPSHOT = ".litellm/provider_models_snapshot.json"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
BENCHMARK_SOURCE = "benchmark"
RUN_KIND = "media"

MEDIA_LEADERBOARD_BEGIN = "<!-- MEDIA-LEADERBOARD:BEGIN -->"
MEDIA_LEADERBOARD_END = "<!-- MEDIA-LEADERBOARD:END -->"
MEDIA_LEADERBOARD_COLUMNS = ("Date", "Run", "Tier", "Model", "Role", "Score", "Detail", "Cost",
                             "Verdict")
MEDIA_LEADERBOARD_MAX_ROWS = 200

VERDICT_RECOMMEND = "recommend"
VERDICT_REJECT = "reject"
VERDICT_NO_BASELINE = "no-baseline"
VERDICT_INCONCLUSIVE = "inconclusive"
VERDICT_BASELINE = "baseline"

# Mirrors image_gen._VISION_GATE_DETAIL: the gate reads renders at "high", so the benchmark does too.
VISION_DETAIL = "high"
# Generous on purpose: a reasoning model (gpt-5-class) bills its thinking against this budget, and an
# empty answer at a tight budget would be a harness artifact, not a measurement (#842). The spend
# estimate charges the FULL budget, so a generous cap here costs estimate headroom, never a surprise.
VISION_MAX_TOKENS = 1200
FIXTURE_SIZE = 512
IMAGE_SIZE = "1024x1024"
DEFAULT_IMAGE_QUALITY = "medium"  # env_constants.IMAGE_QUALITY's default: what production renders at

# ── cost ceilings (USD estimate = tokens x the pinned per-token price) ──
# Prompt text is under ~450 tokens for every call here; 600 is the ceiling charged.
VISION_PROMPT_TOKENS = 600
# Image INPUT tokens at detail=high, the WORST known per-model charge: gpt-4o-mini bills an image at
# ~33x gpt-4o's token count so the price comes out level (2833 base + 5667 per 512px tile). A
# 512px fixture is one tile (8500); a 1024px render is four (25501, rounded up).
VISION_IMAGE_TOKENS_CEILING = {FIXTURE_SIZE: 8500, 1024: 25600}
IMAGE_PROMPT_TOKENS = 250
# Output image tokens for one 1024x1024 render, the max across gpt-image-1/-2 per quality (OpenAI's
# published counts are ~272 / 1056 / 4160 for gpt-image-1; gpt-image-2's medium/high run higher).
IMAGE_OUTPUT_TOKENS_CEILING = {"low": 300, "medium": 1800, "high": 7100}

VISION_MIN_AGREEMENT = 0.8
IMAGE_MIN_ACCEPTANCE = 2 / 3
MAX_CANDIDATES_PER_TIER = 2

# The rubric `image_gen` (#2248) judges renders with - mirrored rather than imported, because
# importing image_gen builds the app's LLM client at import time, which needs credentials that a
# `--dry-run` deliberately does not have. `test_rubric_matches_image_gen` fails the build the moment
# the two drift apart (names, order, floors and the post_image scroll_stop floor).
RUBRIC_CRITERIA = ("specificity", "no_cliche", "thumbnail_read", "text_accuracy", "craft",
                   "scroll_stop", "brand_fit")
RUBRIC_DESCRIPTIONS = {
    "specificity": "depicts THIS brief's concrete idea - its named objects and setting - not merely "
                   "its domain",
    "no_cliche": "free of stock symbols (gears/cogs, pipes/valves, server racks, light bulbs, puzzle "
                 "pieces, handshakes, rockets, glowing brains, cash) and of a generic person-at-laptop "
                 "scene; 5 means none at all",
    "thumbnail_read": "the subject still reads as a 400x225 feed thumbnail",
    "text_accuracy": "every piece of visible text is exactly the text the brief asks for, legibly "
                     "spelled; when the brief asks for no text, 5 means there is none",
    "craft": "clean composition and lighting; no AI artifacts (waxy skin, malformed hands, melted "
             "objects)",
    "scroll_stop": "would make a professional stop scrolling a LinkedIn feed",
    "brand_fit": "reads as professional LinkedIn content: no logos, watermarks or UI chrome",
}
# Acceptable iff every floor holds (#2248's `_RUBRIC_FLOORS`, plus its `scroll_stop >= 4` on the
# post_image surface - the surface these briefs stand in for).
RUBRIC_FLOORS = {"specificity": 4, "no_cliche": 5, "text_accuracy": 4, "craft": 4, "brand_fit": 3,
                 "scroll_stop": 4}

CLICHE_CHOICES = ("gears", "pipes", "server_rack", "light_bulb", "handshake", "rocket",
                  "puzzle_piece")
EMOTION_CHOICES = ("happy", "sad", "angry", "surprised", "neutral", "none")
VISION_FIELDS = ("stray_text", "cliche_objects", "emotion")

_CLICHE_SYNONYMS = {"gear": "gears", "cog": "gears", "cogs": "gears", "pipe": "pipes",
                    "piping": "pipes", "server": "server_rack", "servers": "server_rack",
                    "server_racks": "server_rack", "rack": "server_rack", "lightbulb": "light_bulb",
                    "light_bulbs": "light_bulb", "puzzle": "puzzle_piece",
                    "puzzle_pieces": "puzzle_piece", "rockets": "rocket", "handshakes": "handshake"}

VISION_PROMPT = (
    "You are inspecting ONE image for a LinkedIn image-quality gate. Respond with ONLY a JSON "
    "object:\n"
    '{"stray_text": true|false, "cliche_objects": [...], "emotion": "..."}\n'
    "- stray_text: true if ANY letters, words or numbers are visible anywhere in the image.\n"
    "- cliche_objects: every stock-symbol object you can see, chosen ONLY from this list: "
    + ", ".join(CLICHE_CHOICES) + ". An empty list if none.\n"
    "- emotion: the emotion a face in the image shows, one of "
    + ", ".join(EMOTION_CHOICES[:-1]) + '; "none" if there is no face.')

IMAGE_JUDGE_PROMPT = (
    "You are grading ONE AI-generated image intended as professional LinkedIn visual content.\n"
    "The brief it was rendered from: {brief}\n"
    "Text the brief asks for: {expected_text}\n\n"
    "Score each criterion from 1 (fails) to 5 (excellent):\n{criteria}\n\n"
    "Respond with ONLY a JSON object with an integer 1-5 for every criterion and a short notes "
    'string, e.g. {{"specificity": 4, ..., "notes": "..."}}.')

# Synthetic briefs: ordinary professional scenes, one with requested text and two without, chosen
# so a model that reaches for the stock clichés loses `no_cliche` and one that garbles words loses
# `text_accuracy`. No customer content.
IMAGE_BRIEFS = (
    {"id": "brief-warehouse-route",
     "prompt": "Overhead photo of a logistics coordinator's hands marking a delivery route in red "
               "grease pencil across a printed warehouse floor plan taped to a steel workbench, "
               "early-morning side light. No text, no logos.",
     "expected_text": None},
    {"id": "brief-bakery-chalkboard",
     "prompt": "Photo of a small bakery counter: a slate chalkboard sign that reads exactly "
               "\"FRESH TODAY\" in hand-lettered chalk, beside a wire tray of croissants, warm "
               "window light. No other text, no logos.",
     "expected_text": "FRESH TODAY"},
    {"id": "brief-clinic-handoff",
     "prompt": "Photo of two nurses at a hospital corridor workstation mid-handoff, one pointing at "
               "a printed patient-flow board with coloured magnets, focused expressions, natural "
               "fluorescent light. No readable text, no logos.",
     "expected_text": None},
)


# ─────────────────────────────── env helpers ───────────────────────────────

def max_spend_usd() -> float:
    """The per-run dollar cap (`BENCHMARK_MAX_SPEND_USD`, default $2.00, never negative)."""
    raw = (os.environ.get("BENCHMARK_MAX_SPEND_USD") or "").strip()
    try:
        return max(0.0, float(raw)) if raw else DEFAULT_MAX_SPEND_USD
    except ValueError:
        return DEFAULT_MAX_SPEND_USD


# ─────────────────────────── vision fixtures (pure) ───────────────────────────

VISION_FIXTURES = (
    {"id": "v01-text-headline", "draw": ("text:QUARTERLY RESULTS|UP 42%",),
     "truth": {"stray_text": True, "cliche_objects": [], "emotion": "none"}},
    {"id": "v02-abstract-clean", "draw": ("abstract",),
     "truth": {"stray_text": False, "cliche_objects": [], "emotion": "none"}},
    {"id": "v03-gears", "draw": ("gears",),
     "truth": {"stray_text": False, "cliche_objects": ["gears"], "emotion": "none"}},
    {"id": "v04-pipes", "draw": ("pipes",),
     "truth": {"stray_text": False, "cliche_objects": ["pipes"], "emotion": "none"}},
    {"id": "v05-server-rack", "draw": ("server_rack",),
     "truth": {"stray_text": False, "cliche_objects": ["server_rack"], "emotion": "none"}},
    {"id": "v06-face-happy", "draw": ("face:happy",),
     "truth": {"stray_text": False, "cliche_objects": [], "emotion": "happy"}},
    {"id": "v07-face-sad", "draw": ("face:sad",),
     "truth": {"stray_text": False, "cliche_objects": [], "emotion": "sad"}},
    {"id": "v08-face-surprised", "draw": ("face:surprised",),
     "truth": {"stray_text": False, "cliche_objects": [], "emotion": "surprised"}},
    {"id": "v09-gears-with-text", "draw": ("gears", "text:AUTOMATE EVERYTHING"),
     "truth": {"stray_text": True, "cliche_objects": ["gears"], "emotion": "none"}},
    {"id": "v10-face-with-sign", "draw": ("face:happy", "text:WE ARE HIRING"),
     "truth": {"stray_text": True, "cliche_objects": [], "emotion": "happy"}},
)


def _font(size: int):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except (TypeError, OSError):  # Pillow without FreeType: the bitmap font still draws text
        return ImageFont.load_default()


def _draw_text(draw, spec: str, has_other: bool) -> None:
    lines = spec.split("|")
    top = 410 if has_other else 170
    for i, line in enumerate(lines):
        size = 40 if has_other else 52
        font = _font(size)
        # Shrink until the line fits: a clipped word is still text, but the ground truth should
        # describe exactly what was drawn.
        while size > 12 and hasattr(draw, "textlength") and \
                draw.textlength(line, font=font) > FIXTURE_SIZE - 80:
            size -= 4
            font = _font(size)
        draw.text((40, top + i * 60), line, fill=(20, 24, 33), font=font)


def _draw_gear(draw, cx: int, cy: int, radius: int, teeth: int, fill: tuple) -> None:
    import math
    points = []
    steps = teeth * 4
    for i in range(steps):
        angle = 2 * math.pi * i / steps
        r = radius if (i % 4) in (0, 1) else radius * 0.78
        points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    draw.polygon(points, fill=fill)
    hole = radius * 0.3
    draw.ellipse((cx - hole, cy - hole, cx + hole, cy + hole), fill=(236, 240, 245))


def _draw_pipes(draw) -> None:
    metal, flange = (120, 128, 140), (80, 86, 96)
    draw.rectangle((40, 150, 330, 200), fill=metal)          # horizontal run
    draw.rectangle((290, 150, 340, 440), fill=metal)         # vertical drop (elbow)
    draw.rectangle((290, 400, 480, 450), fill=metal)         # second horizontal run
    for x0, y0, x1, y1 in ((150, 138, 166, 212), (278, 300, 352, 316), (410, 388, 426, 462)):
        draw.rectangle((x0, y0, x1, y1), fill=flange)        # bolted flanges at the joints
    draw.ellipse((170, 70, 250, 150), outline=(180, 40, 40), width=10)  # valve hand-wheel
    draw.line((210, 70, 210, 150), fill=(180, 40, 40), width=8)
    draw.line((210, 150, 210, 160), fill=flange, width=12)


def _draw_server_rack(draw) -> None:
    draw.rectangle((150, 40, 362, 472), fill=(28, 30, 36))
    for unit in range(9):
        top = 56 + unit * 46
        draw.rectangle((164, top, 348, top + 36), fill=(52, 56, 66))
        for slot in range(6):
            x = 176 + slot * 18
            draw.line((x, top + 8, x, top + 28), fill=(30, 32, 38), width=4)
        draw.ellipse((318, top + 12, 330, top + 24), fill=(40, 220, 90))
        draw.ellipse((300, top + 12, 312, top + 24), fill=(60, 140, 255))


def _draw_face(draw, emotion: str, has_other: bool) -> None:
    cx, cy, r = 256, (190 if has_other else 256), (150 if has_other else 190)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(250, 204, 60), outline=(120, 90, 20),
                 width=6)
    eye_y, eye_dx, eye_r = cy - r * 0.25, r * 0.38, r * 0.1
    for side in (-1, 1):
        ex = cx + side * eye_dx
        draw.ellipse((ex - eye_r, eye_y - eye_r, ex + eye_r, eye_y + eye_r), fill=(40, 30, 10))
    mouth_w = r * 0.55
    if emotion == "happy":
        draw.arc((cx - mouth_w, cy - r * 0.15, cx + mouth_w, cy + r * 0.6), 20, 160,
                 fill=(90, 30, 10), width=10)
    elif emotion == "sad":
        # A plain frown and a tear: slanted brows read as ANGRY at this scale, which would make
        # the ground truth wrong rather than the model.
        draw.arc((cx - mouth_w, cy + r * 0.3, cx + mouth_w, cy + r * 0.95), 200, 340,
                 fill=(90, 30, 10), width=10)
        tx, ty = cx + eye_dx, eye_y + r * 0.14
        draw.ellipse((tx - r * 0.05, ty, tx + r * 0.05, ty + r * 0.16), fill=(70, 150, 230))
    elif emotion == "surprised":
        mr = r * 0.17
        draw.ellipse((cx - mr, cy + r * 0.3 - mr, cx + mr, cy + r * 0.3 + mr * 1.4),
                     fill=(90, 30, 10))
        for side in (-1, 1):  # high, round brows
            ex = cx + side * eye_dx
            draw.arc((ex - r * 0.16, eye_y - r * 0.42, ex + r * 0.16, eye_y - r * 0.18), 200, 340,
                     fill=(90, 60, 10), width=7)


def _draw_abstract(draw) -> None:
    for band in range(8):
        shade = 200 - band * 14
        draw.rectangle((0, band * 64, FIXTURE_SIZE, band * 64 + 64), fill=(shade, shade + 20, 235))
    draw.ellipse((90, 120, 250, 280), fill=(255, 160, 90))
    draw.ellipse((260, 220, 440, 400), fill=(90, 190, 170))
    draw.polygon(((300, 60), (440, 180), (330, 200)), fill=(240, 240, 250))


def render_fixture(fixture: dict) -> bytes:
    """Draw one vision fixture as PNG bytes - deterministic for a given Pillow build.

    Args:
        fixture: An entry of ``VISION_FIXTURES``.

    Returns:
        The PNG file content.
    """
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (FIXTURE_SIZE, FIXTURE_SIZE), (236, 240, 245))
    draw = ImageDraw.Draw(image)
    elements = list(fixture["draw"])
    has_other = len(elements) > 1
    for element in elements:
        kind, _, arg = element.partition(":")
        if kind == "text":
            _draw_text(draw, arg, has_other)
        elif kind == "gears":
            _draw_gear(draw, 190, 170 if has_other else 210, 110, 10, (110, 118, 130))
            _draw_gear(draw, 345, 290 if has_other else 330, 80, 8, (150, 120, 70))
        elif kind == "pipes":
            _draw_pipes(draw)
        elif kind == "server_rack":
            _draw_server_rack(draw)
        elif kind == "face":
            _draw_face(draw, arg, has_other)
        elif kind == "abstract":
            _draw_abstract(draw)
        else:
            raise ValueError(f"unknown fixture element {element!r}")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


# ─────────────────────────── vision scoring (pure) ───────────────────────────

def _strip_fence(text: str) -> str:
    stripped = str(text or "").strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.S)
    return fenced.group(1) if fenced else stripped


def parse_json_object(text: Optional[str]) -> Optional[dict]:
    """A model's JSON answer, tolerating a markdown fence; None when it is not one object."""
    if not text:
        return None
    try:
        value = json.loads(_strip_fence(text))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def normalize_cliches(values) -> list:
    """Model-reported cliché objects mapped onto ``CLICHE_CHOICES`` names, sorted, de-duplicated."""
    if not isinstance(values, list):
        return []
    out = set()
    for raw in values:
        key = re.sub(r"[\s-]+", "_", str(raw or "").strip().lower())
        key = _CLICHE_SYNONYMS.get(key, key)
        if key and key != "none":
            out.add(key)
    return sorted(out)


def score_vision_answer(truth: dict, answer: Optional[dict]) -> dict:
    """Agreement of one answer with one fixture's ground truth.

    Args:
        truth: The fixture's ``truth``.
        answer: The parsed JSON answer, or None when the model returned nothing parseable.

    Returns:
        ``{"fields": {field: bool}, "agreement": float, "parsed": bool}``. An unparseable answer
        scores every field wrong: production parses the same JSON, so it is a real failure.
    """
    if answer is None:
        return {"fields": {f: False for f in VISION_FIELDS}, "agreement": 0.0, "parsed": False}
    stray = answer.get("stray_text")
    emotion = str(answer.get("emotion") or "none").strip().lower()
    fields = {
        "stray_text": isinstance(stray, bool) and stray == truth["stray_text"],
        "cliche_objects": normalize_cliches(answer.get("cliche_objects"))
        == sorted(truth["cliche_objects"]),
        "emotion": emotion == truth["emotion"],
    }
    return {"fields": fields, "agreement": sum(fields.values()) / len(fields), "parsed": True}


def vision_scorecard(model: str, role: str, results: list) -> dict:
    """Aggregate one model's per-fixture results into a scorecard.

    Args:
        model: The model id.
        role: ``champion`` or ``candidate``.
        results: ``[{"case", "score"|None, "error"|None, "latency_ms", "cost_usd"}]``.

    Returns:
        Agreement overall and per field; errored cases count as unmeasured, not as zero.
    """
    measured = [r for r in results if r.get("score") is not None]
    total_fields = len(measured) * len(VISION_FIELDS)
    hits = sum(sum(r["score"]["fields"].values()) for r in measured)
    per_field = {f: (sum(1 for r in measured if r["score"]["fields"][f]) / len(measured)
                     if measured else None) for f in VISION_FIELDS}
    return {"tier": "lem-vision", "model": model, "role": role, "cases": len(results),
            "measured": len(measured), "errors": len(results) - len(measured),
            "unparsed": sum(1 for r in measured if not r["score"]["parsed"]),
            "agreement_rate": (hits / total_fields) if total_fields else None,
            "field_rates": per_field,
            "exact_cases": sum(1 for r in measured if r["score"]["agreement"] == 1.0),
            "latency_p50_ms": _median([r.get("latency_ms") for r in results]),
            "cost_usd": round(sum(r.get("cost_usd") or 0.0 for r in results), 6)}


# ─────────────────────────── image scoring (pure) ───────────────────────────

def image_judge_messages(brief: dict, image_bytes: bytes) -> list:
    """The judge's one message: rubric prompt plus the render as a data URL."""
    criteria = "\n".join(f"- {name}: {RUBRIC_DESCRIPTIONS[name]}" for name in RUBRIC_CRITERIA)
    text = IMAGE_JUDGE_PROMPT.format(
        brief=brief["prompt"], criteria=criteria,
        expected_text=(f'exactly "{brief["expected_text"]}"' if brief.get("expected_text")
                       else "none - any visible text is a defect"))
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return [{"role": "user", "content": [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}",
                                            "detail": VISION_DETAIL}}]}]


def parse_rubric(text: Optional[str]) -> Optional[dict]:
    """The judge's scores, STRICTLY: every criterion an integer 1-5, else None (unscored).

    A partial or out-of-range answer is never read as a zero - that would charge the judge's
    failure to the image model being measured.
    """
    answer = parse_json_object(text)
    if answer is None:
        return None
    scores = {}
    for name in RUBRIC_CRITERIA:
        value = answer.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
            return None
        scores[name] = value
    return scores


def rubric_acceptable(scores: dict) -> bool:
    """Whether one judged render clears every #2248 floor."""
    return all(scores.get(name, 0) >= floor for name, floor in RUBRIC_FLOORS.items())


def image_scorecard(model: str, role: str, results: list) -> dict:
    """Aggregate one image model's per-brief results.

    Args:
        model: The image model id.
        role: ``champion`` or ``candidate``.
        results: ``[{"brief", "scores"|None, "error"|None, "latency_ms", "cost_usd"}]``; a brief
            with ``error`` never rendered, one with ``scores`` None rendered but went unscored.

    Returns:
        Acceptance rate and mean per criterion over JUDGED briefs only.
    """
    judged = [r for r in results if r.get("scores")]
    means = {name: (sum(r["scores"][name] for r in judged) / len(judged) if judged else None)
             for name in RUBRIC_CRITERIA}
    overall = (sum(means.values()) / len(means)) if judged else None
    return {"tier": "lem-image", "model": model, "role": role, "cases": len(results),
            "rendered": sum(1 for r in results if not r.get("error")),
            "measured": len(judged), "errors": sum(1 for r in results if r.get("error")),
            "unscored": sum(1 for r in results if not r.get("error") and not r.get("scores")),
            "acceptance_rate": (sum(1 for r in judged if rubric_acceptable(r["scores"]))
                                / len(judged)) if judged else None,
            "criterion_means": means, "overall_mean": overall,
            "latency_p50_ms": _median([r.get("latency_ms") for r in results]),
            "cost_usd": round(sum(r.get("cost_usd") or 0.0 for r in results), 6)}


def _median(values: list) -> Optional[float]:
    clean = sorted(v for v in values if isinstance(v, (int, float)))
    if not clean:
        return None
    mid = len(clean) // 2
    return clean[mid] if len(clean) % 2 else (clean[mid - 1] + clean[mid]) / 2


# ─────────────────────────────── gate (pure) ───────────────────────────────

def gate_media(candidate: dict, champion: Optional[dict]) -> dict:
    """Champion/challenger verdict for one media scorecard.

    Vision: the agreement rate must clear ``VISION_MIN_AGREEMENT`` AND meet-or-beat the champion.
    Image: the acceptance rate must clear ``IMAGE_MIN_ACCEPTANCE`` AND meet-or-beat the champion on
    acceptance and on overall mean. A side with unmeasured cases is ``inconclusive`` - a partial
    read must not render as a full verdict.

    Args:
        candidate: A scorecard from ``vision_scorecard`` / ``image_scorecard``.
        champion: The same tier's champion scorecard, or None.

    Returns:
        ``{"tier", "model", "champion", "verdict", "reasons"}``.
    """
    tier = candidate["tier"]
    out = {"tier": tier, "model": candidate["model"],
           "champion": champion["model"] if champion else None, "reasons": []}
    key = "agreement_rate" if tier == "lem-vision" else "acceptance_rate"
    floor = VISION_MIN_AGREEMENT if tier == "lem-vision" else IMAGE_MIN_ACCEPTANCE
    rate = candidate.get(key)
    if rate is None or candidate["measured"] < candidate["cases"]:
        out["verdict"] = VERDICT_INCONCLUSIVE
        out["reasons"].append(f"{candidate['cases'] - candidate['measured']} of "
                              f"{candidate['cases']} cases unmeasured")
        return out
    if champion is None or champion.get(key) is None or champion["measured"] < champion["cases"]:
        out["verdict"] = VERDICT_NO_BASELINE
        out["reasons"].append("no fully measured champion to compare against")
        if rate < floor:
            out["reasons"].append(f"{key} {rate:.0%} below the {floor:.0%} floor")
        return out
    if rate < floor:
        out["reasons"].append(f"{key} {rate:.0%} below the {floor:.0%} floor")
    if rate < champion[key]:
        out["reasons"].append(f"{key} {rate:.0%} < champion {champion[key]:.0%}")
    if tier == "lem-image" and (candidate["overall_mean"] or 0) < (champion["overall_mean"] or 0):
        out["reasons"].append(f"rubric mean {candidate['overall_mean']:.2f} < champion "
                              f"{champion['overall_mean']:.2f}")
    out["verdict"] = VERDICT_REJECT if out["reasons"] else VERDICT_RECOMMEND
    return out


def gate_cards(cards: list) -> list:
    """Every candidate scorecard gated against its tier's champion."""
    champions = {c["tier"]: c for c in cards if c["role"] == "champion"}
    return [gate_media(c, champions.get(c["tier"])) for c in cards if c["role"] == "candidate"]


def harness_outage(run: dict) -> Optional[str]:
    """Why a run measured NOTHING (every case of every model errored), else None (#923)."""
    cards = run.get("scorecards") or []
    if not cards or any(c["measured"] or (c["tier"] == "lem-image" and c["rendered"])
                        for c in cards):
        return None
    errors = [r.get("error") for t in (run.get("results") or {}).values()
              for rows in t.values() for r in rows if r.get("error")]
    common = max(set(errors), key=errors.count) if errors else "no case was measured"
    return f"harness outage: every case of every model failed ({common})"


# ─────────────────────────── roster + spend plan (pure) ───────────────────────────

def tier_deployments(deployments: list, tier: str) -> list:
    """The bare model ids serving ``tier``, in config (routing) order."""
    return [d["bare"] for d in deployments if d.get("group") == tier]


def resolve_roster(deployments: list, tiers: list, overrides: Optional[dict] = None,
                   provider_snapshot: Optional[dict] = None, today: Optional[str] = None) -> dict:
    """Who runs per media tier: the champion and up to ``MAX_CANDIDATES_PER_TIER`` candidates.

    The champion is the tier's FIRST deployment. With no override, candidates are the tier's other
    deployments plus the newest same-family successor the committed provider snapshot lists - and
    anything with a published sunset is dropped, because measuring a model weeks from removal buys
    nothing. An explicit override is taken as given.

    Args:
        deployments: Rows from ``model_health_check.parse_deployments``.
        tiers: Media tiers to resolve.
        overrides: ``{tier: [model, ...]}`` from the CLI.
        provider_snapshot: The committed ``.litellm/provider_models_snapshot.json`` document.
        today: ISO date; a sunset on or before it, or after it, both count as published.

    Returns:
        ``{tier: {"champion": str|None, "candidates": [str]}}``.
    """
    import provider_model_scan as pms  # noqa: WPS433 - sibling script
    snapshot = provider_snapshot or {}
    sunsetting = {d.get("model") for d in snapshot.get("deprecations") or [] if d.get("date")}
    models = {"openai": (snapshot.get("openai") or {}).get("models") or [],
              "perplexity": (snapshot.get("perplexity") or {}).get("models") or []}
    upgrades = {u["current"]: u for u in pms.plan_provider_upgrades(
        deployments, models, snapshot.get("deprecations") or [])}
    roster = {}
    for tier in tiers:
        serving = tier_deployments(deployments, tier)
        champion = serving[0] if serving else None
        if overrides and overrides.get(tier):
            candidates = [m for m in overrides[tier] if m != champion]
        else:
            pool = list(serving[1:])
            if champion and champion in upgrades:
                pool.append(upgrades[champion]["candidate"])
            candidates = [m for m in pool if m not in sunsetting]
        seen: list = []
        for name in candidates:
            if name not in seen:
                seen.append(name)
        roster[tier] = {"champion": champion, "candidates": seen[:MAX_CANDIDATES_PER_TIER]}
    return roster


def price_spec(model: str, prices: dict) -> Optional[dict]:
    """The pinned cost-map entry for a bare OpenAI model (``openai/<id>`` first, then ``<id>``)."""
    for key in (f"openai/{model}", model):
        spec = (prices or {}).get(key)
        if isinstance(spec, dict):
            return spec
    return None


def _num(value) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def vision_call_cost(model: str, prices: dict, image_px: int = FIXTURE_SIZE) -> Optional[float]:
    """Upper-bound USD for one vision call, or None when the model is unpriced."""
    spec = price_spec(model, prices) or {}
    cin, cout = _num(spec.get("input_cost_per_token")), _num(spec.get("output_cost_per_token"))
    if cin is None or cout is None:
        return None
    tokens_in = VISION_PROMPT_TOKENS + VISION_IMAGE_TOKENS_CEILING[image_px]
    return tokens_in * cin + VISION_MAX_TOKENS * cout


def image_render_cost(model: str, prices: dict, quality: str) -> Optional[float]:
    """Upper-bound USD for one 1024x1024 render, or None when the model is unpriced."""
    spec = price_spec(model, prices) or {}
    cout = _num(spec.get("output_cost_per_image_token"))
    cin = _num(spec.get("input_cost_per_token"))
    if cout is None or cin is None or quality not in IMAGE_OUTPUT_TOKENS_CEILING:
        return None
    return IMAGE_PROMPT_TOKENS * cin + IMAGE_OUTPUT_TOKENS_CEILING[quality] * cout


def plan_spend(roster: dict, prices: dict, *, judge_model: Optional[str],
               quality: str = DEFAULT_IMAGE_QUALITY) -> dict:
    """Price every planned call before any is made.

    Args:
        roster: From ``resolve_roster``.
        prices: The pinned cost map's ``models``.
        judge_model: The model that judges renders (the ``lem-vision`` champion).
        quality: The gpt-image quality to render at.

    Returns:
        ``{"items": [{tier, model, role, calls, est_usd}], "total_usd", "unpriced": [model]}``.
        ``total_usd`` is None when anything is unpriced - a bound nobody can compute.
    """
    items, unpriced = [], []
    for tier, entry in roster.items():
        models = ([(entry["champion"], "champion")] if entry.get("champion") else []) + \
            [(m, "candidate") for m in entry.get("candidates") or []]
        for model, role in models:
            if tier == "lem-vision":
                calls = len(VISION_FIXTURES)
                per = vision_call_cost(model, prices)
                if per is None:
                    unpriced.append(model)
            else:
                calls = len(IMAGE_BRIEFS)
                render = image_render_cost(model, prices, quality)
                judge = vision_call_cost(judge_model, prices, 1024) if judge_model else None
                if render is None:
                    unpriced.append(model)
                if judge is None:
                    unpriced.append(f"{judge_model or 'no judge'} (judge)")
                per = None if render is None or judge is None else render + judge
            items.append({"tier": tier, "model": model, "role": role, "calls": calls,
                          "est_usd": None if per is None else round(per * calls, 6)})
    unpriced = sorted(set(unpriced))
    total = None if unpriced else round(sum(i["est_usd"] for i in items), 6)
    return {"items": items, "total_usd": total, "unpriced": unpriced, "quality": quality,
            "judge_model": judge_model}


def spend_refusal(plan: dict, cap: float) -> Optional[str]:
    """Why the plan may not run (unpriced model / estimate over cap), else None."""
    if plan["unpriced"]:
        return ("refusing to run: no pinned price for " + ", ".join(plan["unpriced"])
                + " in .litellm/model_prices_snapshot.json - an unpriced model has no spend bound "
                "(refresh it with scripts/model_registry.py --refresh-prices)")
    if plan["total_usd"] > cap:
        return (f"refusing to run: estimated spend ${plan['total_usd']:.2f} exceeds the "
                f"${cap:.2f} cap (--max-spend-usd / BENCHMARK_MAX_SPEND_USD)")
    return None


def render_spend_plan(plan: dict, cap: float) -> str:
    """The planned-spend table printed by ``--dry-run`` and at the top of every real run."""
    lines = [f"Planned media-benchmark spend (cap ${cap:.2f}, image quality "
             f"{plan['quality']}, judge {plan['judge_model'] or 'n/a'}):",
             "| Tier | Model | Role | Calls | Est. USD (upper bound) |", "|---|---|---|---|---|"]
    for item in plan["items"]:
        est = "UNPRICED" if item["est_usd"] is None else f"${item['est_usd']:.4f}"
        lines.append(f"| {item['tier']} | `{item['model']}` | {item['role']} | {item['calls']} "
                     f"| {est} |")
    total = "n/a (unpriced model)" if plan["total_usd"] is None else f"${plan['total_usd']:.4f}"
    lines.append(f"\n**Estimated total:** {total} of a ${cap:.2f} cap")
    refusal = spend_refusal(plan, cap)
    lines.append(f"**Decision:** {refusal or 'within cap - a real run would proceed'}")
    return "\n".join(lines)


# ─────────────────────────────── I/O (mocked in tests) ───────────────────────────

class SpendCapExceeded(RuntimeError):
    """Raised when real usage would take a run past its dollar cap."""


class SpendMeter:
    """Running spend for one run. ``reserve`` refuses a call that could take it past the cap."""

    def __init__(self, cap: float) -> None:
        """Start a meter at zero against ``cap`` dollars."""
        self.cap = cap
        self.spent = 0.0

    def reserve(self, estimate: float) -> None:
        """Refuse a call whose upper-bound estimate would cross the cap."""
        if self.spent + estimate > self.cap + 1e-9:
            raise SpendCapExceeded(f"spend cap ${self.cap:.2f} reached (spent ${self.spent:.4f})")

    def charge(self, usd: float) -> None:
        """Record what a finished call actually cost."""
        self.spent += max(0.0, usd)


class MediaProvider:
    """OpenAI vision + image calls through ``AttributedOpenAI``, the one client.

    Pointed at the provider directly (``OPENAI_BASE_URL``, default api.openai.com) because a
    candidate outside the config has no proxy alias to be reached by; champions go the same way so
    both sides of a comparison share their plumbing.
    """

    def __init__(self, api_key: str, base_url: str = DEFAULT_OPENAI_BASE_URL,
                 timeout: float = 180.0) -> None:
        """Remember the credentials; the client itself is built on first use."""
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self._client = None

    def _openai(self):
        if self._client is None:
            from cqc_lem.utilities.ai.client import AttributedOpenAI  # noqa: WPS433 - lazy: env
            self._client = AttributedOpenAI(api_key=self.api_key, base_url=self.base_url,
                                            timeout=self.timeout)
        return self._client

    def vision(self, model: str, messages: list) -> dict:
        """One vision completion -> ``{text, error, usage, latency_ms}``; never raises."""
        started = time.time()
        try:
            response = self._openai().chat.completions.create(
                model=model, messages=messages, response_format={"type": "json_object"},
                max_completion_tokens=VISION_MAX_TOKENS)
        except Exception as exc:  # noqa: BLE001 - a provider failure is a case result, not a crash
            return {"text": None, "error": str(exc)[:200], "usage": {},
                    "latency_ms": (time.time() - started) * 1000.0}
        usage = getattr(response, "usage", None)
        return {"text": (response.choices[0].message.content or "").strip() or None, "error": None,
                "usage": {"prompt_tokens": getattr(usage, "prompt_tokens", None),
                          "completion_tokens": getattr(usage, "completion_tokens", None)},
                "latency_ms": (time.time() - started) * 1000.0}

    def render(self, model: str, prompt: str, *, quality: str) -> dict:
        """One image render -> ``{image, error, usage, latency_ms}``; never raises."""
        started = time.time()
        try:
            response = self._openai().images.generate(model=model, prompt=prompt, size=IMAGE_SIZE,
                                                      quality=quality, n=1)
            item = (response.data or [None])[0]
            b64 = getattr(item, "b64_json", None) if item is not None else None
            if not b64:
                raise RuntimeError("render returned no b64 image data")
            image = base64.b64decode(b64)
        except Exception as exc:  # noqa: BLE001
            return {"image": None, "error": str(exc)[:200], "usage": {},
                    "latency_ms": (time.time() - started) * 1000.0}
        usage = getattr(response, "usage", None)
        return {"image": image, "error": None,
                "usage": {"input_tokens": getattr(usage, "input_tokens", None),
                          "output_tokens": getattr(usage, "output_tokens", None)},
                "latency_ms": (time.time() - started) * 1000.0}


def _vision_actual(model: str, prices: dict, usage: dict, fallback: float) -> float:
    spec = price_spec(model, prices) or {}
    pin, pout = usage.get("prompt_tokens"), usage.get("completion_tokens")
    cin, cout = _num(spec.get("input_cost_per_token")), _num(spec.get("output_cost_per_token"))
    if isinstance(pin, int) and isinstance(pout, int) and cin is not None and cout is not None:
        return pin * cin + pout * cout
    return fallback


def _render_actual(model: str, prices: dict, usage: dict, fallback: float) -> float:
    spec = price_spec(model, prices) or {}
    tin, tout = usage.get("input_tokens"), usage.get("output_tokens")
    cin, cout = _num(spec.get("input_cost_per_token")), _num(spec.get("output_cost_per_image_token"))
    if isinstance(tin, int) and isinstance(tout, int) and cin is not None and cout is not None:
        return tin * cin + tout * cout
    return fallback


def run_media_benchmark(roster: dict, *, provider: "MediaProvider", prices: dict, cap: float,
                        judge_model: Optional[str], run_id: str, today: str,
                        quality: str = DEFAULT_IMAGE_QUALITY,
                        log: Callable[[str], None] = lambda _m: None) -> dict:
    """Run every planned case. The caller has already passed ``spend_refusal``.

    Args:
        roster: From ``resolve_roster``.
        provider: The I/O client.
        prices: The pinned cost map's ``models``.
        cap: Dollar cap; the meter stops the run before a call that could cross it.
        judge_model: The ``lem-vision`` champion that grades renders.
        run_id: Stable id for the report.
        today: ISO date.
        quality: gpt-image quality.
        log: Progress sink (stderr in the CLI).

    Returns:
        The results document ``write_media_report`` renders.
    """
    meter = SpendMeter(cap)
    results: dict = {}
    cards: list = []
    stopped = None
    for tier, entry in roster.items():
        models = ([(entry["champion"], "champion")] if entry.get("champion") else []) + \
            [(m, "candidate") for m in entry.get("candidates") or []]
        results[tier] = {}
        for model, role in models:
            rows: list = []
            if tier == "lem-vision":
                for fixture in VISION_FIXTURES:
                    est = vision_call_cost(model, prices) or 0.0
                    try:
                        meter.reserve(est)
                    except SpendCapExceeded as exc:
                        stopped = str(exc)
                        rows.append({"case": fixture["id"], "score": None, "error": stopped})
                        continue
                    encoded = base64.b64encode(render_fixture(fixture)).decode("ascii")
                    answer = provider.vision(model, [{"role": "user", "content": [
                        {"type": "text", "text": VISION_PROMPT},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/png;base64,{encoded}", "detail": VISION_DETAIL}}]}])
                    cost = _vision_actual(model, prices, answer.get("usage") or {}, est)
                    meter.charge(cost)
                    rows.append({"case": fixture["id"], "error": answer["error"],
                                 "answer": answer["text"],
                                 "score": None if answer["error"] else score_vision_answer(
                                     fixture["truth"], parse_json_object(answer["text"])),
                                 "latency_ms": answer["latency_ms"], "cost_usd": round(cost, 6)})
                card = vision_scorecard(model, role, rows)
            else:
                for brief in IMAGE_BRIEFS:
                    est_render = image_render_cost(model, prices, quality) or 0.0
                    est_judge = vision_call_cost(judge_model, prices, 1024) or 0.0
                    try:
                        meter.reserve(est_render + est_judge)
                    except SpendCapExceeded as exc:
                        stopped = str(exc)
                        rows.append({"brief": brief["id"], "scores": None, "error": stopped})
                        continue
                    rendered = provider.render(model, brief["prompt"], quality=quality)
                    cost = _render_actual(model, prices, rendered.get("usage") or {}, est_render)
                    row = {"brief": brief["id"], "error": rendered["error"], "scores": None,
                           "latency_ms": rendered["latency_ms"]}
                    if not rendered["error"]:
                        verdict = provider.vision(judge_model,
                                                  image_judge_messages(brief, rendered["image"]))
                        cost += _vision_actual(judge_model, prices, verdict.get("usage") or {},
                                               est_judge)
                        row["scores"] = parse_rubric(verdict["text"])
                        row["judge_error"] = verdict["error"]
                        if row["scores"]:
                            row["acceptable"] = rubric_acceptable(row["scores"])
                    meter.charge(cost)
                    row["cost_usd"] = round(cost, 6)
                    rows.append(row)
                card = image_scorecard(model, role, rows)
            log(f"  {tier} {model} ({role}): measured {card['measured']}/{card['cases']}, "
                f"${card['cost_usd']:.4f}")
            results[tier][model] = rows
            cards.append(card)
    run = {"kind": RUN_KIND, "source": BENCHMARK_SOURCE, "run_id": run_id, "date": today,
           "roster": roster, "judge_model": judge_model, "quality": quality,
           "results": results, "scorecards": cards, "gates": gate_cards(cards),
           "spend": {"cap_usd": cap, "actual_usd": round(meter.spent, 6), "stopped": stopped}}
    run["harness_outage"] = harness_outage(run)
    return run


# ─────────────────────────────── rendering (pure) ───────────────────────────────

def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def assert_renderable(run: dict) -> None:
    """Refuse a run that is not benchmark output, or that measured nothing (#923)."""
    if run.get("kind") != RUN_KIND or run.get("source") != BENCHMARK_SOURCE:
        raise ValueError("not a media benchmark run - refusing to publish untagged output")
    outage = harness_outage(run)
    if outage:
        raise ValueError(outage)


def render_media_report(run: dict) -> str:
    """The per-run markdown report (no image bytes, no prompts beyond the synthetic briefs)."""
    assert_renderable(run)
    verdicts = {(g["tier"], g["model"]): g for g in run.get("gates") or []}
    spend = run.get("spend") or {}
    lines = [f"# Media benchmark `{run['run_id']}` ({run['date']})", "",
             f"**Tiers:** {', '.join(run['roster'])} · **Judge:** `{run.get('judge_model')}` · "
             f"**Image quality:** {run.get('quality')} · **Spend:** "
             f"${spend.get('actual_usd', 0):.4f} of a ${spend.get('cap_usd', 0):.2f} cap"
             + (f" · ⛔ stopped early: {spend['stopped']}" if spend.get("stopped") else ""), "",
             ("Synthetic fixtures only (issue #2251). Produced by `scripts/benchmark_models.py "
              "--tiers lem-vision,lem-image`; a `recommend` is advisory and is never applied to "
              "`.litellm/config.yaml` automatically."), ""]
    for tier in run["roster"]:
        cards = [c for c in run["scorecards"] if c["tier"] == tier]
        if not cards:
            continue
        lines += [f"## {tier}", ""]
        if tier == "lem-vision":
            lines += [("| Model | Role | Agreement | Stray text | Cliché | Emotion | Exact cases | "
                       "Unparsed | Errors | p50 | Cost | Verdict |"),
                      "|---|---|---|---|---|---|---|---|---|---|---|---|"]
            for c in cards:
                g = verdicts.get((tier, c["model"]))
                fr = c["field_rates"]
                lines.append(
                    f"| `{c['model']}` | {c['role']} | {_pct(c['agreement_rate'])} | "
                    f"{_pct(fr['stray_text'])} | {_pct(fr['cliche_objects'])} | "
                    f"{_pct(fr['emotion'])} | {c['exact_cases']}/{c['cases']} | {c['unparsed']} | "
                    f"{c['errors']} | {_ms(c['latency_p50_ms'])} | ${c['cost_usd']:.4f} | "
                    f"{g['verdict'] if g else VERDICT_BASELINE} |")
        else:
            lines += ["| Model | Role | Accepted | Mean | " + " | ".join(RUBRIC_CRITERIA)
                      + " | Unscored | Errors | Cost | Verdict |",
                      "|---|---|---|---|" + "---|" * len(RUBRIC_CRITERIA) + "---|---|---|---|"]
            for c in cards:
                g = verdicts.get((tier, c["model"]))
                means = " | ".join(_mean(c["criterion_means"][n]) for n in RUBRIC_CRITERIA)
                lines.append(
                    f"| `{c['model']}` | {c['role']} | {_pct(c['acceptance_rate'])} | "
                    f"{_mean(c['overall_mean'])} | {means} | {c['unscored']} | {c['errors']} | "
                    f"${c['cost_usd']:.4f} | {g['verdict'] if g else VERDICT_BASELINE} |")
        lines.append("")
        for c in cards:
            g = verdicts.get((tier, c["model"]))
            if g and g["reasons"]:
                lines.append(f"- `{c['model']}` {g['verdict']}: " + "; ".join(g["reasons"]))
        lines += ["", "<details><summary>Per-case detail</summary>", ""]
        for model, rows in (run["results"].get(tier) or {}).items():
            for r in rows:
                if tier == "lem-vision":
                    fields = (r.get("score") or {}).get("fields") or {}
                    misses = [f for f, ok in fields.items() if not ok]
                    status = (f"error: {r['error']}" if r.get("error") else
                              "✅" if not misses else "❌ " + ", ".join(misses))
                    lines.append(f"- `{model}` {r['case']}: {status}")
                else:
                    status = (f"error: {r['error']}" if r.get("error") else
                              "unscored (judge answer unparseable)" if not r.get("scores") else
                              ("✅ " if r.get("acceptable") else "❌ ")
                              + ", ".join(f"{k} {v}" for k, v in r["scores"].items()))
                    lines.append(f"- `{model}` {r['brief']}: {status}")
        lines += ["", "</details>", ""]
    return "\n".join(lines).rstrip() + "\n"


def _ms(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.0f} ms"


def _mean(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def media_leaderboard_rows(run: dict) -> list:
    """One leaderboard row per scorecard."""
    verdicts = {(g["tier"], g["model"]): g["verdict"] for g in run.get("gates") or []}
    rows = []
    for c in run.get("scorecards") or []:
        if c["tier"] == "lem-vision":
            fr = c["field_rates"]
            score, detail = _pct(c["agreement_rate"]), (
                f"text {_pct(fr['stray_text'])} · cliché {_pct(fr['cliche_objects'])} · "
                f"emotion {_pct(fr['emotion'])}")
        else:
            score, detail = _pct(c["acceptance_rate"]), f"mean {_mean(c['overall_mean'])}/5"
        rows.append({"date": run["date"], "run_id": run["run_id"], "tier": c["tier"],
                     "model": c["model"], "role": c["role"], "score": score, "detail": detail,
                     "cost": f"${c['cost_usd']:.4f}",
                     "verdict": verdicts.get((c["tier"], c["model"]), VERDICT_BASELINE)})
    return rows


def _row_md(row: dict) -> str:
    return (f"| {row['date']} | `{row['run_id']}` | {row['tier']} | `{row['model']}` | "
            f"{row['role']} | {row['score']} | {row['detail']} | {row['cost']} | {row['verdict']} |")


def parse_media_row(line: str) -> Optional[dict]:
    """A media leaderboard row back into a dict; header/divider rows are None."""
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) != len(MEDIA_LEADERBOARD_COLUMNS):
        return None
    if [c.strip("`") for c in cells] == list(MEDIA_LEADERBOARD_COLUMNS):
        return None
    if all(c and set(c) <= {"-", ":"} for c in cells):
        return None
    keys = ("date", "run_id", "tier", "model", "role", "score", "detail", "cost", "verdict")
    row = dict(zip(keys, cells))
    row["run_id"], row["model"] = row["run_id"].strip("`"), row["model"].strip("`")
    return row


def update_media_leaderboard(text: str, rows: list) -> str:
    """Merge rows into the media leaderboard block; a re-rendered run replaces its own rows."""
    header = ["| " + " | ".join(MEDIA_LEADERBOARD_COLUMNS) + " |",
              "|" + "---|" * len(MEDIA_LEADERBOARD_COLUMNS)]
    body = str(text or "")
    if MEDIA_LEADERBOARD_BEGIN in body and MEDIA_LEADERBOARD_END in body:
        before, rest = body.split(MEDIA_LEADERBOARD_BEGIN, 1)
        inner, after = rest.split(MEDIA_LEADERBOARD_END, 1)
    else:
        before, inner, after = body.rstrip() + "\n\n## Media leaderboard\n\n", "", "\n"
    existing = [r for r in (parse_media_row(line) for line in inner.splitlines()
                            if line.strip().startswith("|")) if r]
    fresh = {(r["run_id"], r["tier"], r["model"]) for r in rows}
    kept = [r for r in existing if (r["run_id"], r["tier"], r["model"]) not in fresh]
    merged = (list(rows) + kept)[:MEDIA_LEADERBOARD_MAX_ROWS]
    table = "\n".join(header + [_row_md(r) for r in merged])
    return f"{before}{MEDIA_LEADERBOARD_BEGIN}\n{table}\n{MEDIA_LEADERBOARD_END}{after}"


def report_filename(run: dict) -> str:
    """``<date>-<run_id>.md`` beside the README, like the text reports."""
    return f"{run['date']}-{run['run_id']}.md"


def write_media_report(run: dict, out_dir: str) -> str:
    """Write the report and merge the media leaderboard; returns the report path."""
    body = render_media_report(run)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, report_filename(run))
    with open(path, "w") as fh:
        fh.write(body)
    readme = os.path.join(out_dir, "README.md")
    existing = ""
    if os.path.exists(readme):
        with open(readme) as fh:
            existing = fh.read()
    with open(readme, "w") as fh:
        fh.write(update_media_leaderboard(existing, media_leaderboard_rows(run)))
    return path
