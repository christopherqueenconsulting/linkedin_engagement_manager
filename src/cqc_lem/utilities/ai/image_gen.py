"""ONE renderer facade for AI still images (newsletter covers, post images, thumbnails).

Backend policy (IMAGE_BACKEND):
- ``auto`` (default): gpt-image via the LiteLLM ``lem-image`` group first, FLUX via Replicate
  when that fails. The proxied call rides the attributed client, so PostHog/cost routing see it
  with zero extra plumbing.
- ``gpt-image`` / ``flux``: force one backend (no cross-fallback).

Avatar likeness deliberately does NOT render here — ``ai_helper.generate_post_image`` owns the
LoRA path (guardrails, C2PA, disclosure flags) and calls into this module only for the
non-avatar case.

The vision quality gate (``render_image_gated``) is bounded (IMAGE_GATE_MAX_ATTEMPTS total
renders) and fails OPEN: a vision outage must never take a cover or post image down with it —
for covers the human ``pending_review`` gate still sits behind this one.
"""

import base64
import dataclasses
import os
import re
import secrets
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Optional

from cqc_lem import assets_dir
from cqc_lem.utilities.ai.client import client
from cqc_lem.utilities.env_constants import (
    DEFAULT_IMAGE_MODEL,
    IMAGE_BACKEND,
    IMAGE_GATE_MAX_ATTEMPTS,
    IMAGE_QUALITY,
    IMAGE_QUALITY_GATE_SURFACES,
)
from cqc_lem.utilities.logger import log_debug, log_info, log_warning

# gpt-image accepts exactly these; anything else falls back to square. 4:5 is NOT a gpt-image size
# (gpt-image-2 may take other multiples of 16, but nothing here relies on that): it renders at the
# portrait size and `conform_to_ratio` crops the result (top-protecting) before the judge sees it.
_SIZE_BY_RATIO = {
    "1:1": "1024x1024",
    "16:9": "1536x1024",
    "9:16": "1024x1536",
    "4:5": "1024x1536",
}

# Ratios a render is cropped to after it lands, as (width, height) parts. 1024x1536 -> 1024x1280.
_CROP_RATIOS = {"4:5": (4, 5)}
# A render already within this of the target aspect is left alone (FLUX takes "4:5" natively).
_CROP_TOLERANCE = 0.01
# Where a portrait render loses its excess height (#2249 gauntlet): post images put the hook in the
# top third, and a CENTRED 1024x1536 -> 1024x1280 crop cut 128px off the top — the headline's top
# line on 2 of 5 renders. So the top loses 48px and the bottom 208px; on any other height the excess
# is split in the same 48:208 proportion. Width trims (too-wide renders) stay centred.
_CROP_TOP_PX = 48
_CROP_BOTTOM_PX = 208

# Replicate renders are bounded so a hung prediction can't stall a Celery worker forever.
_REPLICATE_TIMEOUT_SECONDS = int(os.getenv("REPLICATE_TIMEOUT_SECONDS", "300"))
_REPLICATE_ATTEMPTS = 2

_GENERATED_SUBDIR = os.path.join("images", "generated")

# Appended to EVERY render prompt, whatever wrote it. The brief author is told to avoid marks,
# but that only governs what it writes — gpt-image-2 inserts a LinkedIn logo into business scenes
# entirely on its own, and the two covers it did that to both reached review carrying someone
# else's trademark. Belongs here rather than in the brief so a hand-written or retried prompt
# cannot lose it.
#
# BACKEND-AWARE on purpose (BFL prompting docs): gpt-image is instruction-following, so an
# explicit prohibition works. FLUX has no negative prompting and largely ignores negation —
# naming "logos" in a FLUX prompt can SUMMON one — so FLUX renders get the same constraint
# phrased positively instead.
# Round 9 (#2241): gpt-image painted the brief's role and group nouns as labels — a poster
# reading "Founders & Content Teams In Conversation", badges saying "SECURITY" — so both
# backends now refuse every label-carrying surface by name.
_NO_LABELS = (" No captions, titles, posters, signage, name badges, lanyards with text, or labels "
              "of any kind.")
# Round 11: no brief named a laptop, yet "AI routing" and "smaller model servers" rendered laptops
# showing code, server racks and cables — the image model INFERS tech hardware from the topic.
# So every render, on every surface and backend, states the scene positively and then explicitly.
NO_TECH_CLAUSE = (" The scene contains no computers, laptops, screens, servers, cables or code; "
                  "people interact with each other in a real place.")
_NO_MARKS_GPT = (" Absolutely no text, letters, words, numbers, captions, watermarks, logos, "
                 "brand marks, app icons, social-media icons, charts, or UI elements anywhere "
                 "in the image." + _NO_LABELS + NO_TECH_CLAUSE)
_NO_MARKS_FLUX = (" Every garment and surface is plain and unbranded, screens are blank, walls "
                  "clean and unmarked." + _NO_LABELS + NO_TECH_CLAUSE)

# No render carries text — not even a headline (issue #2241, round 6): ``image_compose`` typesets
# the hook onto the finished render, so this constraint holds on every surface without exception.

# The blanket constraint above is not enough when the scene NAMES a surface whose whole purpose is
# carrying marks (issue #1376). Newsletter cover ed9 went through this exact path — brief authored
# by image_brief, rendered with `_NO_MARKS_GPT` appended, then through the vision gate — and the
# laptop in it came back with four logo tiles and the letters "AI" on its screen, legible at feed
# width. A described screen invites the renderer to fill it with plausible UI, and a general
# prohibition does not reliably suppress that; what does is saying what the surface DOES show.
#
# So these clauses are POSITIVE on both backends, not just on FLUX: they state the surface's
# appearance rather than forbidding its contents, which is the one phrasing that survives a
# renderer that ignores negation AND steers one that does not.
#
# Split by surface class on purpose. A prompt naming a laptop must not be handed a clause that
# names posters and signs — on FLUX naming a thing summons it, so a screen-only scene would gain
# a poster it never asked for.
_MARK_MAGNETS: tuple[tuple[str, str], ...] = (
    (r"screen|screens|monitor|monitors|laptop|laptops|display|displays|tablet|tablets|phone|"
     r"phones|smartphone|smartphones|television|televisions|projector|projectors|kiosk|kiosks|"
     r"dashboard|dashboards",
     " Every screen in the frame is switched off and uniformly dark, its glass reflecting only "
     "the light already in the room."),
    (r"whiteboard|whiteboards|chalkboard|chalkboards|blackboard|blackboards|board|boards|poster|"
     r"posters|sign|signs|signage|banner|banners|billboard|billboards|notebook|notebooks|page|"
     r"pages|book|books|document|documents|paper|papers",
     " Every board, page and printed surface in the frame is bare, smooth and evenly blank."),
)
_MARK_MAGNET_PATTERNS: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(rf"\b(?:{words})\b", re.IGNORECASE), clause) for words, clause in _MARK_MAGNETS)


def with_no_marks(prompt: str, backend: str = "gpt-image", *,
                  scene: Optional[str] = None) -> str:
    """The render prompt plus the no-marks constraint for that backend, added at most once.

    A prompt that NAMES a mark-carrying surface — a screen, a whiteboard, a page — also gets that
    surface's blank-state clause, because the blanket constraint measurably did not hold there
    (issue #1376). The match runs against the SCENE, with either blanket constraint stripped back
    out first: `_NO_MARKS_FLUX` itself says "screens are blank", so matching the whole string would
    make every FLUX render look like it described a screen.

    Args:
        prompt: the text actually handed to the renderer; the clause is appended to THIS.
        backend: ``flux`` or ``gpt-image``; decides the blanket constraint's phrasing.
        scene: the AUTHOR's brief, when the prompt is no longer only that. A repair round appends
            `repair_directive`, whose own counter says "screens blank" and whose gpt-image phrasing
            quotes the gate's issue strings verbatim ("garbled text on whiteboard") — matching on
            that would hand a screenless satchel scene a clause naming a screen, on the backend
            where naming a thing summons it. Defaults to ``prompt``.
    """
    suffix = _NO_MARKS_FLUX if backend == "flux" else _NO_MARKS_GPT
    if _NO_MARKS_GPT in prompt or _NO_MARKS_FLUX in prompt:
        marked = prompt
    else:
        marked = f"{prompt}{suffix}"
    source = prompt if scene is None else scene
    source = source.replace(_NO_MARKS_GPT, "").replace(_NO_MARKS_FLUX, "")
    for pattern, clause in _MARK_MAGNET_PATTERNS:
        if clause not in marked and pattern.search(source):
            marked = f"{marked}{clause}"
    return marked


# What a rejected render must show INSTEAD, keyed by what the vision gate actually reports. Its
# issue strings name the DEFECT ("garbled text on whiteboard", "six fingers on the left hand"),
# and the repair round used to paste them straight back into the next prompt — which is fine for
# instruction-following gpt-image and actively harmful on FLUX, where naming a thing summons it
# and negation is largely ignored. So the retry was re-requesting the exact defect it rejected
# (issue #1141). Same backend split, and same reason, as _NO_MARKS_* above.
_REPAIR_COUNTERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("text", "letter", "word", "caption", "writing", "typograph", "watermark", "logo",
      "brand", "icon", "chart", "ui "),
     "every garment and surface plain and unmarked, screens blank, walls clean"),
    (("hand", "finger", "thumb", "knuckle", "digit"),
     "hands relaxed and out of frame, the subject framed above the waist"),
    # "six fingers" is only ONE way the gate phrases bad anatomy; "malformed face", "extra limb"
    # and "distorted torso" all describe the same repair and matched nothing at first.
    (("anatomy", "anatomical", "face", "limb", "torso", "distort", "deform", "malform",
      "uncanny", "mangl", "fused", "merging", "extra "),
     "one whole, naturally proportioned subject cleanly separated from its surroundings"),
    (("relevance", "relate", "abstract", "filler", "generic", "unrelated", "meaningless"),
     "a literal, concrete depiction of {focal}, in a real setting"),
)
_REPAIR_FALLBACK = "one clear, concrete subject in a real setting, cleanly lit and plainly framed"


def repair_directive(issues: list, backend: str = "gpt-image",
                     focal_concept: Optional[str] = None) -> str:
    """The re-render clause for a render the vision gate rejected, phrased for that backend.

    gpt-image is told what to AVOID (it follows instructions); FLUX is told what to SHOW, because
    a FLUX prompt that names a defect renders it. A verdict this map has no counter for still
    yields a positive directive rather than nothing — an empty retry clause is just the same
    render again.

    Args:
        issues: ``QualityVerdict.issues`` — short phrases naming what was WRONG.
        backend: ``flux`` or ``gpt-image``; decides which phrasing the clause takes.
        focal_concept: the subject the brief asked for, named back on an off-topic verdict —
            repeating "the stated subject" is the vagueness that let it drift.
    """
    cleaned = [str(i).strip() for i in (issues or []) if str(i).strip()]
    if backend != "flux":
        fixes = "; ".join(cleaned) or "low relevance to the subject"
        return (f"The previous render was rejected for: {fixes}. "
                f"Avoid those problems entirely in this render.")
    focal = (focal_concept or "").strip() or "the subject the brief describes"
    lowered = " ".join(cleaned).lower()
    wanted = [counter.format(focal=focal) for triggers, counter in _REPAIR_COUNTERS
              if any(trigger in lowered for trigger in triggers)]
    return "Render this scene again with " + "; ".join(wanted or [_REPAIR_FALLBACK]) + "."


@dataclass
class QualityVerdict:
    """Outcome of the vision gate. ``checked=False`` means the gate could not run (fails open)."""
    acceptable: bool
    checked: bool = True
    relevance: Optional[int] = None
    issues: list = field(default_factory=list)
    # The staged judge's output (issue #2241); empty on the legacy single-call path.
    rubric: dict = field(default_factory=dict)
    blind_description: Optional[str] = None
    failing: list = field(default_factory=list)
    # The judge saw a weak or wrong expression (round 5): the NEXT render — the second candidate
    # and every retry — must push the emotion harder.
    emotion_weak: bool = False
    # The "piques interest" rubric (archetype round): seven 0-2 scores, folded into scroll_stop
    # and specificity, kept whole for the receipt.
    pop: dict = field(default_factory=dict)


def size_for_ratio(ratio: str) -> str:
    """The gpt-image `size` for an aspect ratio; anything unrecognised falls back to square.

    Only the ratios the surfaces render at map to a size the API accepts, so an unknown ratio
    degrades to 1024x1024 rather than being passed through as a size the request would fail on.
    4:5 maps to the portrait size; ``conform_to_ratio`` crops it afterwards.
    """
    return _SIZE_BY_RATIO.get(ratio, "1024x1024")


def conform_to_ratio(path: Optional[str], ratio: str) -> Optional[str]:
    """Crop a render in place to ``ratio`` when that ratio is one no backend renders natively.

    Deterministic: the largest box of the target aspect, so a 1024x1536 gpt-image render becomes
    exactly 1024x1280 for 4:5 — TOP-PROTECTING, not centred: 48px off the top and 208px off the
    bottom (``_CROP_TOP_PX`` / ``_CROP_BOTTOM_PX``), because the hook sits in the top third. A
    too-wide render is trimmed evenly from both sides. Only ratios in ``_CROP_RATIOS`` are touched, and a render
    already at the aspect (FLUX/Replicate takes ``aspect_ratio="4:5"``) is left as it is. Runs
    before the vision judge, so the judge grades what will ship.

    Never raises: an unreadable file is returned unchanged and the gate decides what it is worth.

    Args:
        path: The rendered file, or None when nothing rendered.
        ratio: The ratio the caller asked for, e.g. ``"4:5"``.

    Returns:
        ``path`` (the same file, possibly cropped), or None when ``path`` was None.
    """
    parts = _CROP_RATIOS.get(ratio)
    if not path or not parts:
        return path
    try:
        from PIL import Image
        with Image.open(path) as img:
            width, height = img.size
            target = parts[0] / parts[1]
            if abs(width / height - target) <= _CROP_TOLERANCE:
                return path
            if width / height > target:
                new_w, new_h = int(round(height * target)), height
            else:
                new_w, new_h = width, int(round(width / target))
            excess = height - new_h
            left = (width - new_w) // 2
            top = excess * _CROP_TOP_PX // (_CROP_TOP_PX + _CROP_BOTTOM_PX)
            image_format = img.format
            cropped = img.crop((left, top, left + new_w, top + new_h))
            cropped.load()
        cropped.save(path, format=image_format)
        log_debug("Render cropped to ratio", ratio=ratio, size=f"{new_w}x{new_h}")
    except Exception as e:
        log_warning("Could not crop render to ratio — keeping it uncropped", exc=e, ratio=ratio)
    return path


def _save_image_bytes(data: bytes, user_id: Optional[int], extension: str = ".png") -> str:
    directory = os.path.join(assets_dir, _GENERATED_SUBDIR, str(user_id or "system"))
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"img_{secrets.token_hex(8)}{extension}")
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _render_via_gpt_image(prompt: str, *, ratio: str, quality: Optional[str],
                          user_id: Optional[int], post_id: Optional[int],
                          surface: Optional[str] = None) -> str:
    """One gpt-image render through the proxy. Raises on failure — the caller owns fallback."""
    quality = quality or IMAGE_QUALITY
    size = size_for_ratio(ratio)
    response = client.images.generate(model="lem-image", prompt=prompt, size=size,
                                      quality=quality, n=1)
    item = (response.data or [None])[0]
    if item is None:
        raise RuntimeError("gpt-image returned no image data")

    b64 = getattr(item, "b64_json", None)
    if b64:
        image_bytes = base64.b64decode(b64)
        path = _save_image_bytes(image_bytes, user_id)
    elif getattr(item, "url", None):
        # Older deployments in the group may still hand back a URL.
        from cqc_lem.utilities.utils import save_video_url_to_dir
        directory = os.path.join(assets_dir, _GENERATED_SUBDIR, str(user_id or "system"))
        os.makedirs(directory, exist_ok=True)
        path = save_video_url_to_dir(item.url, directory)
    else:
        raise RuntimeError("gpt-image response carried neither b64_json nor url")

    served_model = getattr(response, "model", None) or "gpt-image-2"
    from cqc_lem.utilities.observability import image_cost_usd, track_media_cost
    track_media_cost("image", "openai", image_cost_usd(1, model=served_model, quality=quality),
                     user_id=user_id, post_id=post_id, qty=1, model=served_model,
                     meta={"size": size, "quality": quality, "surface": surface})
    log_info("Generated image via gpt-image", user_id=user_id, post_id=post_id,
             ai_model=served_model, api_provider="openai", surface=surface)
    return path


def _run_replicate_polled(ref: str, input_params: dict) -> Any:
    """Run one prediction, waiting on it by polling rather than on the create request.

    The SDK default (``wait=True``) sends ``Prefer: wait`` and holds the create POST open for up
    to 60s with a 60.5s read timeout. A slow start (a cold avatar LoRA) overruns that read, so
    ``httpx.ReadTimeout`` fires inside ``create()``, long before the outer bound — and the SDK
    never retries a POST. With ``wait=False`` create returns at once and the wait moves to GET
    polls, which ``timeout_s`` bounds. An iterator output is drained here, so that its lazy polls
    also stay inside the bound.
    """
    import replicate

    output = replicate.run(ref, input=input_params, wait=False)
    return list(output) if isinstance(output, Iterator) else output


def run_replicate_bounded(ref: str, input_params: dict,
                          timeout_s: int = _REPLICATE_TIMEOUT_SECONDS,
                          attempts: int = _REPLICATE_ATTEMPTS):
    """``replicate.run`` with a hard timeout and bounded retry + backoff.

    The bare call blocks until the prediction resolves — a stuck one used to hold a worker
    slot indefinitely, and any transient 5xx was terminal.
    """
    last_error: Exception = RuntimeError("replicate.run was never attempted")
    for attempt in range(1, max(1, attempts) + 1):
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(_run_replicate_polled, ref, input_params)
            return future.result(timeout=timeout_s)
        except FutureTimeout as e:
            last_error = TimeoutError(f"Replicate render exceeded {timeout_s}s")
            log_warning("Replicate render timed out", api_provider="replicate", ai_model=ref)
            _ = e
        except Exception as e:
            last_error = e
            log_warning("Replicate render failed", exc=e, api_provider="replicate", ai_model=ref)
        finally:
            executor.shutdown(wait=False)
        if attempt <= max(1, attempts) - 1:
            time.sleep(2 ** attempt)
    raise last_error


def _render_via_flux(prompt: str, *, ratio: str, image_model: str,
                     user_id: Optional[int], surface: Optional[str] = None) -> str:
    from cqc_lem.utilities.ai.ai_helper import get_flux_image_via_replicate
    _ = user_id  # cost + logging happen inside the replicate helper, keyed off attribution scope
    return get_flux_image_via_replicate(prompt, ref=image_model, aspect_ratio=ratio,
                                       surface=surface)


def _render_with_backend(prompt: str, *, ratio: str = "1:1",
                         quality: Optional[str] = None,
                         user_id: Optional[int] = None,
                         post_id: Optional[int] = None,
                         image_model: str = DEFAULT_IMAGE_MODEL,
                         surface: Optional[str] = None,
                         scene: Optional[str] = None) -> tuple[str, str]:
    """One render, plus the backend that actually produced it (``gpt-image`` or ``flux``).

    Which backend ran is not answerable from configuration: under the default ``auto`` gpt-image
    leads and FLUX silently catches its failures. The gate's repair round has to phrase itself
    for the renderer that will READ it, and a config-derived answer names the defect back at FLUX
    on exactly the runs where gpt-image is down (issue #1141).

    ``scene`` is the author's brief when ``prompt`` carries a repair round on top of it — it is
    what the mark-magnet clauses are matched against (issue #1376).
    """
    backend = (IMAGE_BACKEND or "auto").strip().lower()
    if backend not in ("auto", "gpt-image", "flux"):
        log_warning(f"Unknown IMAGE_BACKEND '{backend}' — using auto")
        backend = "auto"

    if backend in ("auto", "gpt-image"):
        try:
            path = _render_via_gpt_image(with_no_marks(prompt, "gpt-image", scene=scene),
                                         ratio=ratio, quality=quality, user_id=user_id,
                                         post_id=post_id, surface=surface)
            return conform_to_ratio(path, ratio), "gpt-image"
        except Exception as e:
            if backend == "gpt-image":
                raise
            log_warning("gpt-image render failed — falling back to FLUX", exc=e,
                        user_id=user_id, post_id=post_id, api_provider="openai", surface=surface)
    path = _render_via_flux(with_no_marks(prompt, "flux", scene=scene), ratio=ratio,
                            image_model=image_model, user_id=user_id, surface=surface)
    return conform_to_ratio(path, ratio), "flux"


def render_image_from_prompt(prompt: str, *, ratio: str = "1:1",
                             quality: Optional[str] = None,
                             user_id: Optional[int] = None,
                             post_id: Optional[int] = None,
                             image_model: str = DEFAULT_IMAGE_MODEL,
                             surface: Optional[str] = None) -> str:
    """Render one image and return its local file path.

    Never renders a likeness — callers that may include the author go through
    ``generate_post_image`` so the avatar guardrails stay the single decision point.

    ``surface`` is threaded from the caller for media-cost attribution (issue #1291).
    """
    return _render_with_backend(prompt, ratio=ratio, quality=quality, user_id=user_id,
                                post_id=post_id, image_model=image_model,
                                surface=surface)[0]


_VISION_GATE_PROMPT = """You are grading ONE AI-generated image intended as professional \
LinkedIn visual content. The image was generated to depict: {focal}

Grade it strictly. Respond with ONLY a JSON object:
{{"acceptable": true|false, "relevance": 1-5, "issues": ["..."]}}

Mark it unacceptable when ANY of these hold:
- garbled or misspelled text, distorted letters, or gibberish writing anywhere
- deformed anatomy or uncanny distorted objects — inspect HANDS closely: wrong finger
  count, fused or bent-back fingers, mangled knuckles, or a hand merging into an object
- the image does not plausibly relate to the stated subject (relevance 1-2)
- any watermark, logo, brand mark, app icon or UI chrome ANYWHERE in the frame — inspect every
  SCREEN, monitor, laptop display, whiteboard, poster, sign and page closely, at full
  resolution: tiled app icons or a company mark on a laptop screen is the single most common
  way this leaks, and it stays legible when the image is scaled to feed width
- it looks like generic meaningless abstract filler rather than a composed scene
{cliche_rule}Each issue string must be a short actionable phrase (e.g. "garbled text on whiteboard")."""

# Appended only for the `newsletter` surface (issue #1992): five straight covers passed this gate
# at "laptop on desk, relevance 3" because a laptop IS in the same domain as an AI-cost edition —
# just never the edition's actual idea. Named as its own bullet rather than folded into the
# relevance line above, because a lenient vision call answers "acceptable" long before it answers
# "generic" — this gives it a concrete visual pattern to match instead.
_STOCK_OFFICE_CLICHE_RULE = (
    "- a person, torso, or hands positioned at a laptop, desk, or keyboard with a notebook or "
    "coffee mug as the main subject — this is the generic \"person at laptop\" stock-photo scene "
    "and is unacceptable regardless of relevance score\n")
# Below this the render is "in the same domain" but not "of the idea" — a laptop-on-desk cover for
# an AI-cost edition clears a bare relevance>=3 bar every time. Issue #2015: text-post images hit
# the exact same "relates but doesn't depict" failure the newsletter fix (#1992) already solved,
# so the raised floor applies to `post_image` too — only the cliché rule (below) stays
# newsletter-only, since `post_image`'s own preset legitimately wants a person as the subject.
_STRICT_RELEVANCE_SURFACES = frozenset({"newsletter", "post_image"})
_STRICT_MIN_RELEVANCE = 4

# The gate is asked whether the render carries marks, so it has to be able to READ one. At
# `low` the API downsamples to ~512px on the long edge, where four logo tiles on a laptop screen
# in a 1536x1024 cover are simply not resolvable — which is how ed9 passed this gate carrying the
# exact thing it is asked about (issue #1376). Read the cost in DOLLARS, not tokens: `lem-vision`
# leads with gpt-4.1 (gpt-4o-mini as its in-group fallback, issue #2241), and a high-detail look at
# a 1536x1024 cover costs about a cent there — still an order of magnitude below the render it
# grades. The staged judge sends it twice. The gate's own question is worth answering properly.
_VISION_GATE_DETAIL = "high"


def _judge_json(response: Any, which: str) -> dict:
    """The judge's JSON object, tolerant of a fenced reply. Raises when there is none.

    A strict parse turned a fenced reply into an ``unchecked`` pass; an empty one (a spent token
    budget) now says so in the reason instead of as a bare decode error.
    """
    from cqc_lem.utilities.ai.ai_helper import _loads_json_object

    choice = response.choices[0]
    raw = choice.message.content or ""
    parsed = _loads_json_object(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"{which} reply was not a JSON object "
                         f"(finish_reason={getattr(choice, 'finish_reason', None)}, "
                         f"{len(raw)} chars)")
    return parsed


def _unchecked(e: Exception, surface: Optional[str], which: str) -> QualityVerdict:
    """The fail-open verdict for a judge that could not run — LOGGED, with its reason.

    WARNING with ``exc=``, never DEBUG: an unchecked render ships ungraded, and a silent one is
    indistinguishable from a judged pass until someone looks at the image.
    """
    reason = f"{which} judge unavailable: {type(e).__name__}: {e}"[:300]
    log_warning("Image judge could not grade the render — failing open", exc=e,
                surface=surface, action_type="image_gate", judge=which)
    return QualityVerdict(acceptable=True, checked=False, issues=[reason])


def _legacy_inspect(image_path: str, focal_concept: str,
                    surface: Optional[str]) -> QualityVerdict:
    """The single-call gate, graded against the brief's own focal concept (pre-#2241 callers)."""
    try:
        image_part = _image_part(image_path)
        cliche_rule = _STOCK_OFFICE_CLICHE_RULE if surface == "newsletter" else ""
        response = client.chat.completions.create(
            model="lem-vision",
            messages=[{"role": "user", "content": [
                {"type": "text",
                 "text": _VISION_GATE_PROMPT.format(
                     focal=(focal_concept or "professional LinkedIn content")[:400],
                     cliche_rule=cliche_rule)},
                image_part,
            ]}],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=300,
        )
        verdict = _judge_json(response, "legacy judge")
        acceptable = bool(verdict.get("acceptable"))
        relevance = verdict.get("relevance")
        issues = [str(i) for i in (verdict.get("issues") or [])][:6]
        if (surface in _STRICT_RELEVANCE_SURFACES and isinstance(relevance, (int, float))
                and relevance < _STRICT_MIN_RELEVANCE):
            acceptable = False
            if not issues:
                issues = ["relevance below the floor — depicts the domain, not the content's "
                          "actual idea"]
        return QualityVerdict(acceptable=acceptable, relevance=relevance, issues=issues)
    except Exception as e:
        return _unchecked(e, surface, "legacy")


def _image_part(image_path: str) -> dict:
    with open(image_path, "rb") as fh:
        encoded = base64.b64encode(fh.read()).decode("ascii")
    ext = os.path.splitext(image_path)[1].lstrip(".").lower() or "png"
    mime = "jpeg" if ext in ("jpg", "jpeg") else ext
    return {"type": "image_url",
            "image_url": {"url": f"data:image/{mime};base64,{encoded}",
                          "detail": _VISION_GATE_DETAIL}}


# Stage 4a (issue #2241). Shown NOTHING about what the image is for: a judge handed the prompt
# tends to confirm it (FineGRAIN, arXiv 2512.02161), which is how a valve scored 5/5 against "a
# valve symbolising leaks". What a stranger sees is the honest baseline.
BLIND_JUDGE_PROMPT = ("Describe this image in 2 sentences: the main subject, the setting, and "
                      "any objects. Then list EVERY piece of visible text verbatim, each in "
                      "double quotes, including small or garbled text, labels, and text on "
                      "devices or paper — or say there is no visible text.")

# Stage 4b: the targeted questions, asked only AFTER the blind description exists. Since round 6
# they look at the COMPOSITE — the render with its typeset headline — because specificity, scroll
# stop and brand are properties of the cover a scroller sees; the blind look and the stray-text
# check stay on the RAW render, where any text at all is a defect.
_TARGETED_JUDGE_PROMPT = """You are grading ONE AI-generated image for a LinkedIn {surface}.
A viewer who knew nothing about its purpose described the photograph as:
<blind>{blind}</blind>

The image was made for a piece arguing: {thesis}
The cover's kicker (topic tag, typeset by the system): {kicker}
{headline_line}
Read the kicker, headline and scene together as one cover. Look at the image itself and answer:
1. (Advisory only — never part of a score.) Is each of these things visibly depicted?
   {entities}
2. Is any of these stock symbols present: {cliches}?
3. Does the main subject still read as a 400x225 thumbnail? Is the image bright enough, with a
   clear subject, that it stands out in a white social feed?
4. Any AI artifacts — waxy skin, malformed hands, melted or fused objects, garbled lettering?
5. Could this whole cover — kicker, headline and scene together — sit unchanged on an
   unrelated article? Would a viewer get the GIST of the claim above?
6. Does a face show a clear, specific emotion readable at 400x225?
7. Does the visible emotion match "{emotional_beat}" with a {valence} valence ({valence_hint})?
8. Is the emotion authentic rather than exaggerated or cartoonish?
9. Does a gold accent or the charcoal / off-white brand palette read in the image?
10. Does the headline alone name its subject — not just a number? (true when there is none)
11. Is any expression overacted — an open or gaping mouth, a furrowed brow, alarm, a grin at
    bad news?
12. Does it PIQUE INTEREST? Score each 0-2: thumbnail_read (one focal point legible at
   400x225), thesis_fit (it could NOT illustrate the opposite argument), curiosity_gap (shows
   part, withholds part), novelty (an object or number from the reader's world, never stock
   people or a stock symbol), resolves_2s (a stranger "gets it" within 2 seconds with the
   headline), credibility (a sourced number, a real face or stylised art — no AI-person tells),
   icp_relevance (a small-business owner's money, time or risk is visible).
{graphic_note}
Score each criterion 1-5, 5 best:
- specificity: 5 = with its headline, a scroller would correctly guess this piece's argument —
  the kicker + headline name the exact topic and the scene shows the human stakes of it (a
  generic office is fine when kicker, headline and emotion are specific; a 5 needs all three
  working together); 4 = clearly on-topic, slightly generic scene; 3 = the scene could sit on many unrelated posts
  even with the headline; 2 or less = misleading or off-topic.
- no_cliche (5 = no stock symbol at all), thumbnail_read, craft (no artifacts, an authentic
  face), scroll_stop (would it stop a scroll — the question 12 scores, and a readable, fitting
  emotion when there is a face), brand_fit (the brand palette reads).
Respond with ONLY a JSON object:
{{"entities_depicted": {{"<thing>": true}}, "cliches_present": ["..."],
 "thesis_inferable": true, "face_emotion": true, "emotion_matches": true,
 "emotion_authentic": true, "headline_names_subject": true, "bright_enough": true,
 "expression_overacted": false,
 "pop": {{"thumbnail_read": 0, "thesis_fit": 0, "curiosity_gap": 0, "novelty": 0,
         "resolves_2s": 0, "credibility": 0, "icp_relevance": 0}},
 "rubric": {{"specificity": 1, "no_cliche": 1, "thumbnail_read": 1, "craft": 1,
            "scroll_stop": 1, "brand_fit": 1}},
 "issues": ["<short actionable phrase>"]}}"""

_VALENCE_HINTS = {
    "positive": "calm, pleased, relieved or wry — never shock",
    "negative": "concerned, skeptical or frustrated",
    "mixed": "wry or thoughtful",
}

# A judge issue that names a weak expression ("enhance emotional expression", "neutral face").
_EMOTION_ISSUE = re.compile(r"emotion|expression|expressive|neutral|smile|deadpan|blank face",
                            re.IGNORECASE)
# Round 6: "exaggerated like a magazine cover" over-fired into a man wailing over a bill.
EMOTION_DIRECTIVE = ("The expression must be clearly readable at thumbnail size but authentic and "
                     "restrained: {beat}, the face a real person would make, never cartoonish or "
                     "crying.")

RUBRIC_CRITERIA = ("specificity", "no_cliche", "thumbnail_read", "text_accuracy", "craft",
                   "scroll_stop", "brand_fit")
# Acceptable iff every floor holds. `text_accuracy` is decided deterministically from the RAW
# render (None = no text at all, the only right answer); `brand_fit` None means unanswered.
_RUBRIC_FLOORS = {"specificity": 4, "no_cliche": 5, "text_accuracy": 4, "craft": 4,
                  "brand_fit": 3}
# Round 3 (#2241): four rejected covers were people at laptops with neutral faces, scroll_stop 3.
# On the surfaces that have to stop a feed, 3 is a fail.
_SCROLL_STOP_SURFACES = frozenset({"newsletter", "post_image"})
# Round 11: a VIDEO frame with no caption headline (VIDEO_CAPTIONS off, so `burned_caption_text`
# is None) passes specificity at 3. The post text above the video carries the thesis, and a lone
# frame cannot: at 4 every caption-less frame scored 2-3 and every video fell back to Pexels
# stock — a regression from shipping Runway clips. (Superseded below: a CAPTIONED frame takes the
# same floors now.)
# #2241 showcase B (owner-delegated decision): EVERY headline-free AI render — each photo_only
# post and each video frame — failed specificity at 2-3, "thesis not visually inferable". In the
# feed the post text sits directly above that image, so the image is never asked to carry the
# thesis alone. Headline-free renders pass specificity at 3 and must instead be visually strong:
# scroll_stop and craft at 4; no_cliche 5 and stray text stay strict. A render WITH a typeset
# headline keeps specificity 4. Video frames take the photo floors with or without a caption.
_HEADLINE_FREE_SPECIFICITY_FLOOR = 3


def rubric_floors(surface: Optional[str], hook_text: Optional[str],
                  code_drawn: bool = False) -> dict[str, int]:
    """The rubric floors a staged verdict must clear on this surface.

    Args:
        surface: ``newsletter``, ``post_image``, ``video``, ``carousel``…
        hook_text: The headline typeset with the image (a video's burned caption), or None.
        code_drawn: A code-drawn graphic, whose text is correct by construction.

    Returns:
        ``{criterion: floor}``.
    """
    if code_drawn:
        return dict(_CODE_DRAWN_FLOORS)
    floors = dict(_RUBRIC_FLOORS)
    if surface in _SCROLL_STOP_SURFACES:
        floors["scroll_stop"] = _SCROLL_STOP_FLOOR
        floors["thumbnail_read"] = _SCROLL_STOP_FLOOR
    if surface == "video" or (surface == "post_image" and not hook_text):
        floors["specificity"] = _HEADLINE_FREE_SPECIFICITY_FLOOR
        floors["scroll_stop"] = _SCROLL_STOP_FLOOR
        floors["craft"] = max(floors.get("craft", 0), 4)
    return floors
_SCROLL_STOP_FLOOR = 4
_REQUIRED_SCORES = ("specificity", "no_cliche", "craft")
# The "piques interest" rubric (docs/visual-archetypes-research.md §6.4): 0-2 each, ship at >= 9.
POP_CRITERIA = ("thumbnail_read", "thesis_fit", "curiosity_gap", "novelty", "resolves_2s",
                "credibility", "icp_relevance")
POP_SHIP_FLOOR = 9
# A code-drawn graphic's text is typeset from verified facts, so only what code cannot guarantee
# is gated: does it carry THIS piece, would it stop a scroll, does the brand read.
_CODE_DRAWN_FLOORS = {"specificity": 4, "scroll_stop": _SCROLL_STOP_FLOOR, "brand_fit": 3}
_CODE_DRAWN_NOTE = (
    "This cover is a CODE-DRAWN data graphic: every number and label on it was verified against "
    "the article's own sentences and set in the brand font, so its text is correct by "
    "construction. Judge it on whether it carries THIS piece's argument, stops a scroll, and "
    "reads as the brand.\n")


def _pop_scores(raw: Any) -> dict:
    """The judge's seven 0-2 "piques interest" scores, or {} when any is missing or unreadable."""
    if not isinstance(raw, dict):
        return {}
    scores = {}
    for name in POP_CRITERIA:
        value = raw.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return {}
        scores[name] = max(0, min(2, int(value)))
    return scores


def _drawn_archetypes() -> tuple[str, ...]:
    """Every archetype drawn by code: Stage 1's five, plus the post rotation's quote card."""
    from cqc_lem.utilities.ai.image_concept import CODE_DRAWN_ARCHETYPES
    from cqc_lem.utilities.ai.image_graphics import QUOTE_CARD

    return CODE_DRAWN_ARCHETYPES + (QUOTE_CARD,)


def _code_drawn(concept: Any) -> bool:
    return getattr(concept, "archetype", "") in _drawn_archetypes()


def _shows_no_face(concept: Any) -> bool:
    """A code-drawn graphic or an editorial concept: a missing face is right, never a defect."""
    from cqc_lem.utilities.ai.image_concept import ARCHETYPE_EDITORIAL

    return _code_drawn(concept) or getattr(concept, "archetype", "") == ARCHETYPE_EDITORIAL
# The surfaces whose headline ``image_compose`` typesets onto the render. NEVER ``video``: its
# frame's ``hook_text`` is the caption ``video_captions`` burns onto the MP4 later, handed to the
# judge as context only (PR #2249) — compositing it would paint the caption twice.
COMPOSE_SURFACES = frozenset({"newsletter", "post_image"})


def _score(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(1, min(5, int(value)))


def _normalise_text(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split())


# Round 11 (#2241): the blind judge QUOTED its own negative — `Visible text: - "There is no visible
# text."` — and the parser counted that sentence as stray text, failing a clean render.
_NEGATIVE_TEXT = re.compile(
    r"^(?:visible text )?(?:there (?:is|are) )?(?:no|none|nothing)"
    r"(?: (?:visible|legible|readable|discernible))?(?: text| words| lettering| writing)?"
    r"(?: (?:is )?(?:visible|present|shown|at all|in the image|anywhere))*$")


def _no_text_seen(text: str) -> bool:
    normal = _normalise_text(text)
    return (not normal or normal in ("none", "n a", "na", "visible text none")
            or bool(_NEGATIVE_TEXT.match(normal)))


# Text a blind description reports: every quoted string, plus whatever follows a text verb with
# no quotes ("a sign reading OPEN LATE."). "reading" alone is NOT a text verb — "a man reading a
# document" is an activity — so it counts only after a text-bearing noun.
_BLIND_QUOTED = re.compile(r'"([^"]{1,200})"|“([^”]{1,200})”')
_BLIND_TEXT_VERB = re.compile(
    r"(?:\b(?:text|sign|label|tag|caption|words?|title|headline|note|banner)\s+(?:that\s+)?"
    r"(?:reads|reading|says|saying)\b|\b(?:labell?ed|titled|captioned)\b)[\s:,]*"
    r"(?![\s\"“])([^\"“”.;\n]{2,80})", re.IGNORECASE)


def blind_text_strings(blind: str) -> list[str]:
    """Every piece of text a BLIND description says the image carries.

    Args:
        blind: The blind judge's description.

    Returns:
        The quoted strings, then any unquoted text after a text verb, in order.
    """
    found = [(a or b).strip() for a, b in _BLIND_QUOTED.findall(blind or "")]
    found += [m.strip() for m in _BLIND_TEXT_VERB.findall(blind or "")]
    return [t for t in found if t and not _no_text_seen(t)]


def stray_texts(blind: str, hook_text: Optional[str] = None) -> list[str]:
    """Text the blind description saw that is not (part of) ``hook_text``.

    Since round 6 the blind look runs on the RAW render, which must carry no text at all, so the
    gate passes no hook: every transcribed string is stray. ``hook_text`` remains for a caller
    describing an image that legitimately carries one.

    Args:
        blind: The blind judge's description.
        hook_text: Text the image may carry, or None (the raw render: none).

    Returns:
        Each stray string.
    """
    hook = _normalise_text(hook_text or "")
    return [t for t in blind_text_strings(blind)
            if (not hook or _normalise_text(t) not in hook) and not trivial_mark(t, blind)]


# #2241 showcase: video frames failed text_accuracy on an "O" and a clock's numerals — marks no
# reader reads as text. A 1-2 character token that is not a word is ignored, UNLESS the blind look
# itself calls it legible/readable text. Everything longer ("pause", "MONTHLY BILL") stays strict.
_SHORT_WORDS = frozenset({"a", "i", "ai", "ok", "no", "go", "hi", "up", "us", "we", "me", "it",
                          "is", "on", "in", "of", "to", "do", "be", "my", "by", "or", "an", "at",
                          "as", "if", "so", "he", "tv", "pr", "hr", "ux", "ui", "vs"})
_LEGIBLE = re.compile(r"\b(?:legible|readable|clearly (?:reads|written|visible text)|"
                      r"text reads)\b", re.IGNORECASE)


def trivial_mark(text: str, blind: str) -> bool:
    """Is ``text`` a 1-2 character non-word mark the stray-text check should ignore?

    Args:
        text: One string the blind description transcribed.
        blind: The whole blind description, for the legibility exception.

    Returns:
        True for a short non-word token (``"O"``, ``"12"``, ``"3"``) that the blind look did not
        call legible text in the sentence that names it.
    """
    token = _normalise_text(text).replace(" ", "")
    if not token or len(token) > 2 or token in _SHORT_WORDS:
        return False
    for sentence in re.split(r"(?<=[.!?])\s+", blind or ""):
        if text in sentence and _LEGIBLE.search(sentence):
            return False
    return True


def _apply_overlays(rubric: dict, *, blind: str, answer: dict, entities: list,
                    hook_text: Optional[str], face_expected: bool = False,
                    no_face: bool = False, code_drawn: bool = False) -> dict:
    """Deterministic corrections the judge cannot talk its way past.

    ``text_accuracy`` is DECIDED here: any text the blind look found on the raw render is a 2,
    none is n/a. A stock symbol the blind description names, or the judge lists, caps
    ``no_cliche``. No anchor seen, or a gist the headline + image do not convey, caps
    ``specificity`` at 3; a headline that never names its subject caps it at 4. A missing or
    mismatched emotion caps ``scroll_stop``, a cartoonish one caps ``craft``, a dark image caps
    ``thumbnail_read``.

    Archetype round: an image that SHOULD show no face (``no_face``) is never capped for a
    missing or mismatched emotion; a code-drawn graphic's text is correct by construction, so
    its ``text_accuracy`` is n/a. The "piques interest" scores fold in: under
    ``POP_SHIP_FLOOR`` caps ``scroll_stop`` at 3, a zero ``thesis_fit`` caps ``specificity`` at 3.
    """
    from cqc_lem.utilities.ai.image_brief import cliche_hit

    rubric = dict(rubric)
    pop = _pop_scores(answer.get("pop"))
    if pop:
        if code_drawn:
            pop["credibility"] = 2  # every drawn figure traced to its sentence (image_graphics)
        if sum(pop.values()) < POP_SHIP_FLOOR:
            rubric["scroll_stop"] = min(rubric.get("scroll_stop") or 3, 3)
        if pop["thesis_fit"] == 0:
            rubric["specificity"] = min(rubric.get("specificity") or 3, 3)
    cliches = [str(c) for c in (answer.get("cliches_present") or []) if str(c).strip()]
    if cliches or cliche_hit(blind):
        rubric["no_cliche"] = min(rubric.get("no_cliche") or 5, 2)
    rubric["text_accuracy"] = 2 if stray_texts(blind) and not code_drawn else None
    # Round 8: specificity is the judge's ANCHORED DESCRIPTORS on kicker + headline + scene
    # together — nothing deterministic caps it. Anchors seen, the gist and a subject-less
    # headline are advisory issues only (``advisory_issues``): gpt-4.1's "no vendor contract
    # depicted" was asking for exactly the props the brief now refuses to draw.
    if face_expected and answer.get("face_emotion") is False:
        rubric["scroll_stop"] = min(rubric.get("scroll_stop") or 3, 3)
    if answer.get("emotion_matches") is False and not no_face:
        rubric["scroll_stop"] = min(rubric.get("scroll_stop") or 3, 3)
    # Round 6: a wailing man over a bill, caricature faces — a fake emotion is a craft defect.
    if not no_face and (answer.get("emotion_authentic") is False
                        or answer.get("expression_overacted") is True):
        rubric["craft"] = min(rubric.get("craft") or 3, 3)
    if answer.get("bright_enough") is False:
        rubric["thumbnail_read"] = min(rubric.get("thumbnail_read") or 3, 3)
    return rubric


def advisory_issues(answer: dict, entities: list, hook_text: Optional[str]) -> list[str]:
    """The judge's advisory findings — reported on the receipt, never part of a score.

    Args:
        answer: The targeted judge's JSON.
        entities: The anchors it was asked about.
        hook_text: The headline, if any.

    Returns:
        Short issue strings prefixed "advisory:".
    """
    notes = []
    seen = answer.get("entities_depicted") or {}
    if entities and isinstance(seen, dict) and not any(seen.get(e) is True for e in entities):
        notes.append("advisory: no anchor visibly depicted")
    if answer.get("thesis_inferable") is False:
        notes.append("advisory: the gist does not read")
    if hook_text and answer.get("headline_names_subject") is False:
        notes.append("advisory: the headline does not name its subject")
    return notes


def _headline_line(hook_text: Optional[str], surface: Optional[str]) -> str:
    """How the judge is told about the headline this image is read with.

    A cover or post has it typeset onto the composite. A VIDEO frame never does (round 10):
    ``video_captions`` burns the post's opening line(s) into the stored MP4, so the frame is judged
    WITH that caption as its headline — for specificity — while the still itself must carry none.
    """
    if not hook_text:
        return "The cover's headline (typeset onto it by the system): (none — judge the image alone)"
    if surface == "video":
        return (f"The video's opening caption (burned into the video over this frame by the "
                f"system; it is NOT in this still and must not be — judge the frame WITH it as "
                f'its headline): "{hook_text}"')
    return f'The cover\'s headline (typeset onto it by the system): "{hook_text}"'


def _staged_inspect(image_path: str, concept: Any, hook_text: Optional[str],
                    surface: Optional[str], composite_path: Optional[str] = None
                    ) -> QualityVerdict:
    """Stage 4: a BLIND look at the RAW render, then targeted questions on the COMPOSITE."""
    from cqc_lem.utilities.ai.image_brief import CLICHE_OBJECTS, usable_anchors

    code_drawn = _code_drawn(concept)
    try:
        raw_part = _image_part(image_path)
        blind_response = client.chat.completions.create(
            model="lem-vision",
            messages=[{"role": "user", "content": [
                {"type": "text", "text": BLIND_JUDGE_PROMPT}, raw_part]}],
            temperature=0,
            max_tokens=350,
        )
        blind = " ".join(str(blind_response.choices[0].message.content or "").split())[:1200]
        entities = usable_anchors(concept)
        valence = getattr(concept, "valence", "") or "mixed"
        targeted = client.chat.completions.create(
            model="lem-vision",
            messages=[{"role": "user", "content": [
                {"type": "text", "text": _TARGETED_JUDGE_PROMPT.format(
                    surface=surface or "post", blind=blind or "(no description)",
                    thesis=concept.thesis,
                    kicker=(f'"{getattr(concept, "kicker", "")}"' if getattr(concept, "kicker", "")
                            else "(none)"),
                    headline_line=_headline_line(hook_text, surface),
                    emotional_beat=getattr(concept, "emotional_beat", "") or "the piece's mood",
                    valence=valence, valence_hint=_VALENCE_HINTS.get(valence, ""),
                    entities="; ".join(entities) or "(none named)",
                    cliches=", ".join(CLICHE_OBJECTS),
                    graphic_note=_CODE_DRAWN_NOTE if code_drawn else "")},
                _image_part(composite_path) if composite_path else raw_part]}],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=500,
        )
        answer = _judge_json(targeted, "targeted judge")
        raw_rubric = answer.get("rubric") or {}
        rubric = {name: _score(raw_rubric.get(name)) for name in RUBRIC_CRITERIA}
        if any(rubric[name] is None for name in _REQUIRED_SCORES):
            raise ValueError("rubric is missing a required score")
        face_expected = (getattr(concept, "treatment", "") == "people_scene"
                         and not _shows_no_face(concept))
        rubric = _apply_overlays(rubric, blind=blind, answer=answer, entities=entities,
                                 hook_text=hook_text, face_expected=face_expected,
                                 no_face=_shows_no_face(concept), code_drawn=code_drawn)
    except Exception as e:
        return _unchecked(e, surface, "staged")

    pop = _pop_scores(answer.get("pop"))
    if pop and code_drawn:
        pop["credibility"] = 2
    floors = rubric_floors(surface, hook_text, code_drawn)
    failing = [name for name, floor in floors.items()
               if rubric.get(name) is not None and rubric[name] < floor]
    issues = [f"{name} {rubric[name]}/5" for name in failing]
    if pop:
        issues.append(f"pop {sum(pop.values())}/{2 * len(POP_CRITERIA)}")
    if not code_drawn:
        issues += [f"stray text: {t}" for t in stray_texts(blind)][:3]
    issues += advisory_issues(answer, entities, hook_text)
    issues += [str(i) for i in (answer.get("issues") or []) if str(i).strip()]
    emotion_weak = (answer.get("face_emotion") is False or answer.get("emotion_matches") is False
                    or "scroll_stop" in failing
                    or any(_EMOTION_ISSUE.search(str(i)) for i in (answer.get("issues") or [])))
    # A faceless archetype (code-drawn, editorial concept) is never "emotion weak": the emotion
    # repair would ask for a face, which is how an editorial cover shipped a person (#2241).
    return QualityVerdict(acceptable=not failing, relevance=rubric.get("specificity"),
                          issues=issues[:8], rubric=rubric, blind_description=blind,
                          failing=failing,
                          emotion_weak=bool(emotion_weak) and not _shows_no_face(concept),
                          pop=pop)


def inspect_render_quality(image_path: str, focal_concept: str,
                           surface: Optional[str] = None, *,
                           concept: Any = None,
                           hook_text: Optional[str] = None,
                           composite_path: Optional[str] = None) -> QualityVerdict:
    """Vision look at a finished render. Fails OPEN — any error returns acceptable/unchecked.

    With a Stage 1 ``concept`` (issue #2241) the gate is two ``lem-vision`` calls: a BLIND
    description of the RAW render that is shown nothing about the brief or the piece (any text it
    reports is a defect — renders carry none), then targeted questions on the COMPOSITE (the
    render with its typeset headline) — anchors, stock symbols, thumbnail legibility in a white
    feed, artifacts, the gist, the emotion and its valence and authenticity, brand — scored on
    ``RUBRIC_CRITERIA`` with anchored specificity descriptors. Acceptable iff specificity >= 4,
    no_cliche == 5, craft >= 4, brand_fit >= 3, no text in the raw render, and on covers and posts
    scroll_stop and thumbnail_read >= 4. Headline legibility and clipping are guaranteed by
    ``image_compose`` and asserted in its tests, not judged here.

    Without a concept the legacy single call runs: ``surface`` turns on the newsletter-only
    stock-office rule, and for ``newsletter``/``post_image`` (issue #2015) raises the relevance
    floor to ``_STRICT_MIN_RELEVANCE``.

    Args:
        image_path: The RAW render on disk.
        focal_concept: The brief's focal concept — the legacy gate's only yardstick.
        surface: The surface the render is for.
        concept: Stage 1's ``ImageConcept``; switches on the staged judge.
        hook_text: The headline composited onto it, if any.
        composite_path: The composite on disk; the targeted questions look at it.

    Returns:
        The verdict; ``checked=False`` when the gate could not run.
    """
    if concept is None:
        return _legacy_inspect(image_path, focal_concept, surface)
    return _staged_inspect(image_path, concept, hook_text, surface, composite_path)


_RUBRIC_REPAIRS_GPT = {
    "specificity": "show {entities} literally, so the image is plainly about {thesis}",
    "no_cliche": "remove every stock symbol ({cliches}) and build the frame on {entities}",
    "text_accuracy": "{text_fix}",
    "craft": ("natural skin texture, hands relaxed or out of frame, every object whole and solid, "
              "an authentic expression rather than a cartoonish one"),
    "thumbnail_read": ("make the subject larger and simpler, centred in the frame, and light the "
                       "scene bright and high-key so it stands out in a white feed"),
    "scroll_stop": ("a closer framing where {emotion} shows on the face as a specific, authentic "
                    "reaction, a laptop at most a prop"),
    "brand_fit": "one deliberate warm gold accent against the charcoal and off-white palette",
}
# FLUX renders what a prompt NAMES, so its repair never names the defect — only what to show.
_RUBRIC_REPAIRS_FLUX = {
    "specificity": "{entities} shown literally, so the image is plainly about {thesis}",
    "no_cliche": "the whole frame built on {entities}",
    "text_accuracy": "{text_fix}",
    "craft": ("natural skin texture, hands relaxed and out of frame, every object whole and "
              "solid, an authentic, restrained expression"),
    "thumbnail_read": ("the subject larger and simpler, centred in the frame, in bright, "
                       "high-key daylight"),
    "scroll_stop": ("a closer framing where {emotion} shows on the face as a specific, authentic "
                    "reaction"),
    "brand_fit": "one deliberate warm gold accent against a charcoal and off-white palette",
}


# The repairs an OBJECT-ONLY render gets instead: "a closer framing where the emotion shows on the
# face" put a woman on a phone into an editorial_concept cover (#2241 showcase).
_FACELESS_REPAIRS = {
    "scroll_stop": ("a bolder single focal object, larger in the frame, with one unexpected "
                    "oddity that resolves in two seconds — objects only"),
    "craft": "every object whole and solid with crisp edges and real texture — objects only",
    "specificity": "objects that plainly carry {thesis} — objects only",
}


def rubric_repair_directive(verdict: QualityVerdict, backend: str, concept: Any,
                            hook_text: Optional[str] = None) -> str:
    """The re-render clause for a staged-judge rejection, built from its FAILING criteria.

    Args:
        verdict: The staged verdict; ``failing`` names the criteria below their floor.
        backend: ``flux`` or ``gpt-image``; FLUX is only ever told what to SHOW.
        concept: Stage 1's concept, whose entities and thesis the repair names back.
        hook_text: Unused since round 6 — the headline is composited, never rendered; kept for
            call-site compatibility.

    Returns:
        One directive sentence for the next attempt.
    """
    from cqc_lem.utilities.ai.image_brief import usable_anchors

    _ = hook_text
    entities = "; ".join(usable_anchors(concept)[:3]) or "the piece's own specific subject"
    thesis = getattr(concept, "thesis", "") or "the piece's idea"
    text_fix = ("every paper and screen blank and unmarked, every garment and wall plain"
                if backend == "flux" else
                "remove every legible mark from papers and screens; the image carries no text at "
                "all — the headline is typeset later")
    cliches = ", ".join(i for i in verdict.issues if "/5" not in i)[:120] or "generic symbols"
    table = _RUBRIC_REPAIRS_FLUX if backend == "flux" else _RUBRIC_REPAIRS_GPT
    if _shows_no_face(concept):
        # An object-only archetype is never repaired toward a face or a person (#2241 showcase).
        table = dict(table, **_FACELESS_REPAIRS)
    emotion = getattr(concept, "emotional_beat", "") or "the piece's emotion"
    parts = [table[name].format(entities=entities, thesis=thesis, text_fix=text_fix,
                                cliches=cliches, emotion=emotion)
             for name in (verdict.failing or []) if name in table]
    if not parts:
        return repair_directive(verdict.issues, backend, thesis)
    if backend == "flux":
        return "Render this scene again with " + "; ".join(parts) + "."
    return ("The previous render was rejected on: " + ", ".join(verdict.failing)
            + ". Fix it: " + "; ".join(parts) + ".")


# Covers are weekly, so two candidates per attempt is affordable there (round 4); every other
# surface defaults to one.
_DEFAULT_CANDIDATES = {"newsletter": 2}


def _gate_candidates(concept: Any, surface: Optional[str] = None) -> int:
    """How many renders per attempt.

    ``IMAGE_GATE_CANDIDATES`` (read at call time) when set; otherwise 2 for ``newsletter`` and 1
    everywhere else. At most 2, and only for the staged judge — the legacy gate has no rubric to
    rank candidates by. The better rubric total wins.
    """
    if concept is None:
        return 1
    default = _DEFAULT_CANDIDATES.get(surface or "", 1)
    try:
        return max(1, min(2, int(os.getenv("IMAGE_GATE_CANDIDATES", str(default)))))
    except ValueError:
        return default


def _verdict_rank(verdict: QualityVerdict) -> tuple:
    scores = [v for v in (verdict.rubric or {}).values() if isinstance(v, int)]
    return (verdict.acceptable, sum(scores), verdict.relevance or 0)


def _scene_ratio(ratio: str, hook_text: Optional[str], surface: str) -> str:
    """The render's ratio: SQUARE whenever ``image_compose`` will split it beside a type panel.

    Round 8: the split layouts centre-crop a square scene into a 0.9-1.2 aspect region on a 16:9
    or 4:5 canvas, so a centred subject survives either way; any other render keeps its ratio.
    """
    return "1:1" if hook_text and surface in COMPOSE_SURFACES else ratio


def _emotion_beat(concept: Any) -> str:
    """The beat the emotion repair names: good news is spelled out, never "relief" (round 13)."""
    from cqc_lem.utilities.ai.image_brief import POSITIVE_FACE

    if getattr(concept, "valence", "") == "positive":
        return POSITIVE_FACE
    return getattr(concept, "emotional_beat", "") or "the piece's emotion"


def _kicker_for(concept: Any) -> str:
    """Every composite carries a kicker: Stage 1's, else one derived from the concept (round 9)."""
    from cqc_lem.utilities.ai.image_concept import concept_kicker

    return concept_kicker(concept)


def _composite(raw_path: str, hook_text: Optional[str], surface: str, layout: Optional[str],
               brand_kit: Optional[str], kicker: Optional[str] = None,
               signature: Optional[str] = None, panel: Optional[str] = None) -> Optional[str]:
    """The render with its headline typeset on, or None when there is no headline or it fails.

    A compositing failure is a warning, never a lost render: the raw image still ships.
    """
    if not hook_text or surface not in COMPOSE_SURFACES:
        return None
    from cqc_lem.utilities.ai.image_compose import brand_style, compose_headline

    try:
        return compose_headline(raw_path, hook_text, layout=layout,
                                brand=brand_style(brand_kit), surface=surface, kicker=kicker,
                                signature=signature, panel=panel)
    except Exception as e:
        log_warning("Headline compositing failed — shipping the render without it", exc=e,
                    surface=surface, action_type="image_compose")
        return None


def _record_verdict(render_info: Optional[dict], verdict: QualityVerdict) -> str:
    """Write a verdict onto ``render_info`` the way the gate loop does; return its label."""
    label = ("accepted" if verdict.acceptable else "rejected") if verdict.checked else "unchecked"
    if render_info is not None:
        render_info["gate_verdict"] = label
        if verdict.rubric:
            render_info["gate_rubric"] = dict(verdict.rubric)
            render_info["gate_failing"] = list(verdict.failing)
            render_info["gate_blind_description"] = verdict.blind_description
        if verdict.pop:
            render_info["gate_pop"] = dict(verdict.pop)
        if verdict.issues:
            render_info["gate_issues"] = list(verdict.issues)
    return label


def render_code_drawn(concept: Any, *, surface: str, hook_text: Optional[str],
                      layout: Optional[str] = None, brand_kit: Optional[str] = None,
                      signature: Optional[str] = None, user_id: Optional[int] = None,
                      post_id: Optional[int] = None, render_info: Optional[dict] = None,
                      enforce: Optional[bool] = None,
                      panel: Optional[str] = None) -> Optional[str]:
    """Draw the concept's code-drawn archetype(s) and grade the composite — $0 render spend.

    Walks the code-drawn head of ``concept.archetype_ranking``. Each is drawn by
    ``image_graphics`` (which refuses a figure it cannot trace to its sentence, or data it cannot
    set legibly) and graded ONCE by the staged judge on specificity, scroll stop and brand — a
    deterministic drawing would not change on a retry. The first accepted (or ungradable, which
    fails open as every render does) graphic returns. Anything else falls through: None, with
    ``render_info["archetype_fallback_reason"]`` saying why, and the caller renders the chain's AI
    archetype.

    Args:
        concept: Stage 1's concept, after ``select_archetype``.
        surface: ``newsletter`` or ``post_image``.
        hook_text: The headline; no headline, no graphic.
        layout: The rotated split layout.
        brand_kit: The brand clause, for the exact colors.
        signature: The byline.
        user_id: The author.
        post_id: The post, for telemetry.
        render_info: Filled with ``archetype_rendered``, ``graphic_facts`` (every drawn figure
            with its source sentence — the receipt's trace) and the gate fields.
        enforce: As for ``render_image_gated``.
        panel: The headline panel's variant (post ``typeset_card`` rotation); charcoal when None.

    Returns:
        The composite's path, or None to fall back to the AI render.
    """
    from cqc_lem.utilities.ai.image_compose import brand_style
    from cqc_lem.utilities.ai.image_graphics import QUOTE_CARD, GraphicError, render_graphic
    from cqc_lem.utilities.observability import track_image_gate_verdict

    drawable = _drawn_archetypes()
    chain = [a for a in (getattr(concept, "archetype_ranking", ()) or ()) if a in drawable]
    # A quote card carries the post's own sentence, not the headline: it alone needs no hook.
    needs_hook = any(a != QUOTE_CARD for a in chain)
    if (concept is None or not chain or (needs_hook and not hook_text)
            or surface not in COMPOSE_SURFACES or not _code_drawn(concept)):
        return None
    enforced = surface in IMAGE_QUALITY_GATE_SURFACES if enforce is None else enforce
    reasons: list[str] = []
    for archetype in chain:
        out_dir = os.path.join(assets_dir, _GENERATED_SUBDIR, str(user_id or "system"))
        os.makedirs(out_dir, exist_ok=True)
        try:
            drawn = render_graphic(
                archetype, getattr(concept, "graphic", None) or {}, surface=surface,
                hook=hook_text or "", kicker=_kicker_for(concept), signature=signature or "",
                brand=brand_style(brand_kit), layout=layout or getattr(concept, "layout", None),
                out_path=os.path.join(out_dir, f"img_{secrets.token_hex(8)}.png"), panel=panel)
        except GraphicError as e:
            reasons.append(f"{archetype}: {e}")
            log_info("Code-drawn archetype refused — trying the next", user_id=user_id,
                     post_id=post_id, action_type="image_archetype", archetype=archetype,
                     reason=str(e))
            continue
        judged = dataclasses.replace(concept, archetype=archetype)
        # A quote card shows no headline, so the judge is not told one is typeset on it.
        judged_hook = None if archetype == QUOTE_CARD else hook_text
        verdict = inspect_render_quality(drawn.path, getattr(concept, "thesis", "")[:200],
                                         surface=surface, concept=judged, hook_text=judged_hook,
                                         composite_path=drawn.path)
        if not verdict.checked and enforced:
            verdict = inspect_render_quality(drawn.path, getattr(concept, "thesis", "")[:200],
                                             surface=surface, concept=judged,
                                             hook_text=judged_hook, composite_path=drawn.path)
        if verdict.acceptable or not verdict.checked or not enforced:
            label = _record_verdict(render_info, verdict)
            if render_info is not None:
                render_info["archetype_rendered"] = archetype
                render_info["graphic_facts"] = [dict(f) for f in drawn.facts]
                if reasons:
                    render_info["archetype_fallback_reason"] = "; ".join(reasons)
            track_image_gate_verdict(surface=surface, verdict=label, issues=verdict.issues,
                                     attempt_count=1, checked=verdict.checked,
                                     acceptable=verdict.acceptable, user_id=user_id,
                                     post_id=post_id)
            log_info("Code-drawn image rendered", user_id=user_id, post_id=post_id,
                     action_type="image_archetype", archetype=archetype, gate_verdict=label,
                     surface=surface)
            return drawn.path
        reasons.append(f"{archetype}: judge rejected ({', '.join(verdict.failing)})")
        log_info("Code-drawn archetype rejected by the judge — trying the next", user_id=user_id,
                 post_id=post_id, action_type="image_archetype", archetype=archetype,
                 issues="; ".join(verdict.issues))
        try:
            os.remove(drawn.path)
        except OSError as e:
            # A rejected graphic left on disk costs space only; the chain moves on regardless.
            log_debug("Rejected code-drawn graphic not removed", error=str(e),
                      action_type="image_archetype", archetype=archetype)
    if render_info is not None:
        render_info["archetype_fallback_reason"] = "; ".join(reasons)
    return None


def _as_ai_render(concept: Any, render_info: Optional[dict]) -> Any:
    """The concept as its AI archetype, so the judge grades the render that actually shipped."""
    archetype = _ai_archetype(concept)
    if concept is None or not archetype:
        return concept
    if render_info is not None:
        render_info["archetype_rendered"] = archetype
    if getattr(concept, "archetype", "") == archetype or not dataclasses.is_dataclass(concept):
        return concept
    return dataclasses.replace(concept, archetype=archetype)


def _ai_archetype(concept: Any) -> str:
    """The AI archetype a render falls back to: the END of the concept's chain ('' without one)."""
    chain = getattr(concept, "archetype_ranking", ()) or ()
    return chain[-1] if chain else (getattr(concept, "archetype", "") or "")


def _gate_loop(render_once, *, prompt: str, surface: str, focal_concept: Optional[str],
               concept: Any, hook_text: Optional[str], user_id: Optional[int],
               post_id: Optional[int], render_info: Optional[dict],
               log_message: str, layout: Optional[str] = None,
               brand_kit: Optional[str] = None,
               signature: Optional[str] = None,
               enforce: Optional[bool] = None,
               panel: Optional[str] = None) -> Optional[str]:
    """The bounded render → composite → judge → repair loop both gated renderers share.

    ``render_once(current_prompt)`` returns ``(path, backend, info)`` — ``path`` None means the
    render produced nothing and the loop returns None at once; ``info`` is merged into
    ``render_info`` for the candidate actually returned. On covers and posts the headline is
    composited onto each candidate (``image_compose``); the loop returns the COMPOSITE and records
    the raw render as ``render_info["raw_render_path"]``. ``enforce`` overrides the surface's
    membership of ``IMAGE_QUALITY_GATE_SURFACES`` — the video frame passes True, because a frame
    the judge rejects must never be animated whatever the deployment's env says (#2249).
    """
    from cqc_lem.utilities.observability import track_image_gate_verdict

    enforced = surface in IMAGE_QUALITY_GATE_SURFACES if enforce is None else enforce
    attempts = max(1, IMAGE_GATE_MAX_ATTEMPTS) if enforced else 1
    candidates = _gate_candidates(concept, surface)
    current_prompt = prompt
    path: Optional[str] = None
    last_verdict: Optional[QualityVerdict] = None
    layout = layout or getattr(concept, "layout", "") or None

    # Sticky once set (round 5): ed16's second candidate and its retry came out as neutral as the
    # first, because nothing carried the judge's "enhance emotional expression" forward.
    emotion_boost = ""

    for attempt in range(1, attempts + 1):
        best: Optional[tuple] = None
        for _ in range(candidates):
            cand_path, backend, info = render_once(
                f"{current_prompt}\n\n{emotion_boost}" if emotion_boost else current_prompt)
            if not cand_path:
                return None
            composite = _composite(cand_path, hook_text, surface, layout, brand_kit,
                                   kicker=_kicker_for(concept) or None,
                                   signature=signature, panel=panel)
            gate_kwargs = ({"concept": concept, "hook_text": hook_text,
                            "composite_path": composite} if concept is not None else {})
            verdict = inspect_render_quality(cand_path, focal_concept or prompt[:200],
                                             surface=surface, **gate_kwargs)
            if not verdict.checked and enforced:
                # ONE more look before failing open on a surface the gate is meant to hold: a
                # judge blip must not wave an ungraded render through (#2249 gauntlet).
                verdict = inspect_render_quality(cand_path, focal_concept or prompt[:200],
                                                 surface=surface, **gate_kwargs)
            info = dict(info, raw_render_path=cand_path) if composite else info
            if best is None or _verdict_rank(verdict) > _verdict_rank(best[2]):
                best = (composite or cand_path, backend, verdict, info)
            if verdict.acceptable or not verdict.checked:
                break
            if verdict.emotion_weak and concept is not None and not emotion_boost:
                emotion_boost = EMOTION_DIRECTIVE.format(beat=_emotion_beat(concept))
        path, used_backend, verdict, info = best
        if render_info is not None:
            render_info.update(info)
        last_verdict = verdict
        if verdict.acceptable or not verdict.checked:
            break
        log_info(log_message, user_id=user_id, post_id=post_id, action_type="image_gate",
                 surface=surface, attempt=attempt, issues="; ".join(verdict.issues))
        if not enforced or attempt == attempts:
            break
        directive = (rubric_repair_directive(verdict, used_backend, concept)
                     if verdict.failing else
                     repair_directive(verdict.issues, used_backend, focal_concept))
        current_prompt = f"{prompt}\n\n{directive}"

    if last_verdict is not None:
        if last_verdict.checked:
            gate_verdict = "accepted" if last_verdict.acceptable else "rejected"
        else:
            gate_verdict = "unchecked"
        if gate_verdict == "unchecked" and enforced:
            # Availability over strictness, as before — but never silently. The reason rides on
            # the receipt so an ungraded render is visible as one.
            log_info("Render shipped UNCHECKED — the judge could not grade it twice", user_id=user_id,
                     post_id=post_id, action_type="image_gate", surface=surface,
                     reason="; ".join(last_verdict.issues))
        if render_info is not None:
            render_info["gate_verdict"] = gate_verdict
            if last_verdict.rubric:
                # The WHY behind a rejection, for the brief receipt (issue #2241).
                render_info["gate_rubric"] = dict(last_verdict.rubric)
                render_info["gate_failing"] = list(last_verdict.failing)
                render_info["gate_blind_description"] = last_verdict.blind_description
            if last_verdict.pop:
                render_info["gate_pop"] = dict(last_verdict.pop)
            if last_verdict.issues:
                # On the legacy path and for an unchecked verdict too — the WHY either way.
                render_info["gate_issues"] = list(last_verdict.issues)
        track_image_gate_verdict(
            surface=surface,
            verdict=gate_verdict,
            issues=last_verdict.issues,
            attempt_count=attempt,
            checked=last_verdict.checked,
            acceptable=last_verdict.acceptable,
            user_id=user_id,
            post_id=post_id,
        )
    return path


def render_avatar_image_gated(prompt: str, *, avatar: dict, user_id: Optional[int],
                              surface: str, ratio: str = "1:1",
                              focal_concept: Optional[str] = None,
                              post_id: Optional[int] = None,
                              render_info: Optional[dict] = None,
                              concept: Any = None,
                              hook_text: Optional[str] = None,
                              layout: Optional[str] = None,
                              brand_kit: Optional[str] = None,
                              signature: Optional[str] = None,
                              enforce: Optional[bool] = None,
                              panel: Optional[str] = None) -> Optional[str]:
    """LoRA render of an image the author appears in, behind the SAME bounded gate as the base.

    Likeness never renders through the gpt-image path (``generate_post_image`` owns the avatar
    guardrails), so without this the avatar branch was the one path with no quality check at all —
    which is how a post about LLM routing costs came back as a plain headshot against a brick wall.
    Returns the best candidate's path, or None if nothing rendered.

    ``render_info``, when passed, is filled with ``{"used_avatar": bool}`` for the render actually
    RETURNED (issue #1430). ``posts.avatar_media`` cannot answer that question: it is a sticky
    per-POST flag that any earlier avatar render sets and nothing clears, so on a re-render or a
    gate retry that fell back to base Flux it still reads true — which would file a fallback
    frame's likeness verdict under the LoRA render it is meant to be separated from. It also
    carries ``gate_verdict`` (and, under the staged judge, ``gate_rubric`` and friends) for the
    same reason: the verdict is only in hand here, and the brief receipt stored beside the render
    (issue #1377) is what makes it auditable afterwards.

    ``concept``/``hook_text``/``layout``/``brand_kit`` (issue #2241) switch on the staged blind
    judge and the composited headline, exactly as for ``render_image_gated``, and ``panel`` too.
    """
    from cqc_lem.utilities.ai.ai_helper import _record_avatar_media
    from cqc_lem.utilities.avatar.attributes import apply_subject_clause
    from cqc_lem.utilities.avatar.replicate_avatar import generate_image_with_avatar

    drawn = render_code_drawn(concept, surface=surface, hook_text=hook_text, layout=layout,
                              brand_kit=brand_kit, signature=signature, user_id=user_id,
                              post_id=post_id, render_info=render_info, enforce=enforce,
                              panel=panel)
    if drawn:
        if render_info is not None:
            render_info["used_avatar"] = False
        return drawn
    concept = _as_ai_render(concept, render_info)

    def render_once(current_prompt: str) -> tuple:
        # The likeness path talks to Replicate directly, so it never passes through
        # render_image_from_prompt where the constraint is otherwise added. Always FLUX.
        marked = with_no_marks(current_prompt, "flux", scene=prompt)
        path, used_avatar = generate_image_with_avatar(
            apply_subject_clause(marked, avatar), avatar["model_ref"],
            ratio=_scene_ratio(ratio, hook_text, surface), fallback_prompt=marked,
            surface=surface)
        path = conform_to_ratio(path, _scene_ratio(ratio, hook_text, surface)) if path else path
        if used_avatar and path:
            # Provenance for a synthetic likeness of a real person.
            _record_avatar_media(path, post_id, user_id)
        if not path and render_info is not None:
            render_info["used_avatar"] = bool(used_avatar)
        return path, "flux", {"used_avatar": bool(used_avatar)}

    return _gate_loop(render_once, prompt=prompt, surface=surface, focal_concept=focal_concept,
                      concept=concept, hook_text=hook_text, user_id=user_id, post_id=post_id,
                      render_info=render_info, log_message="Avatar image failed the quality gate",
                      layout=layout, brand_kit=brand_kit, signature=signature,
                      enforce=enforce, panel=panel)


def render_image_gated(prompt: str, *, surface: str, ratio: str = "1:1",
                       focal_concept: Optional[str] = None,
                       quality: Optional[str] = None,
                       user_id: Optional[int] = None,
                       post_id: Optional[int] = None,
                       image_model: str = DEFAULT_IMAGE_MODEL,
                       render_info: Optional[dict] = None,
                       concept: Any = None,
                       hook_text: Optional[str] = None,
                       layout: Optional[str] = None,
                       brand_kit: Optional[str] = None,
                       signature: Optional[str] = None,
                       enforce: Optional[bool] = None,
                       panel: Optional[str] = None) -> str:
    """Render with the bounded vision gate. Returns the best candidate's path.

    Surfaces outside IMAGE_QUALITY_GATE_SURFACES get one advisory-only pass (verdict logged,
    render kept). After the attempt budget, the last render ships — for covers the human
    review queue is still behind this gate.

    ``render_info``, when passed, is filled with ``{"gate_verdict": "accepted"|"rejected"|
    "unchecked"}`` — the same out-param shape the avatar renderer uses. The verdict exists only
    inside this call, and recording it beside the stored render (issue #1377) is what lets a later
    audit tell a render the gate passed from one it never looked at.

    ``concept`` (issue #2241) switches on the staged blind judge and its rubric; the repair round
    is then built from the rubric's failing criteria, and ``IMAGE_GATE_CANDIDATES`` > 1 renders
    two candidates per attempt and keeps the better. On covers and posts ``hook_text`` is
    composited onto every candidate by ``image_compose`` (in ``layout``, in ``brand_kit``'s exact
    colors) — the render itself carries no text — and the COMPOSITE is what returns, with the
    concept's kicker above the headline and ``signature`` as a byline (round 7). ``panel`` picks
    the type panel's variant (``image_compose.PANEL_VARIANTS``) for a post ``typeset_card``.
    """
    drawn = render_code_drawn(concept, surface=surface, hook_text=hook_text, layout=layout,
                              brand_kit=brand_kit, signature=signature, user_id=user_id,
                              post_id=post_id, render_info=render_info, enforce=enforce,
                              panel=panel)
    if drawn:
        return drawn
    concept = _as_ai_render(concept, render_info)

    def render_once(current_prompt: str) -> tuple:
        # The backend that RENDERED, not the one configured: under `auto` a gpt-image failure
        # falls through to FLUX, and the retry has to be phrased for whichever one answered.
        # `scene=prompt`: from attempt 2 `current_prompt` carries the repair round too, and the
        # mark-magnet clauses must stay derived from what the AUTHOR described (issue #1376).
        path, backend = _render_with_backend(current_prompt,
                                             ratio=_scene_ratio(ratio, hook_text, surface),
                                             quality=quality,
                                             user_id=user_id, post_id=post_id,
                                             image_model=image_model, surface=surface,
                                             scene=prompt)
        return path, backend, {}

    return _gate_loop(render_once, prompt=prompt, surface=surface, focal_concept=focal_concept,
                      concept=concept, hook_text=hook_text, user_id=user_id, post_id=post_id,
                      render_info=render_info, log_message="Image failed the quality gate",
                      layout=layout, brand_kit=brand_kit, signature=signature,
                      enforce=enforce, panel=panel)
