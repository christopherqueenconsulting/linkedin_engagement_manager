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
_NO_MARKS_GPT = (" Absolutely no text, letters, words, numbers, captions, watermarks, logos, "
                 "brand marks, app icons, social-media icons, charts, or UI elements anywhere "
                 "in the image.")
_NO_MARKS_FLUX = (" Every garment and surface is plain and unbranded, screens are blank, walls "
                  "clean and unmarked.")

# The ONE text exception (issue #2241): an `editorial_graphic` brief carries a declared 2-5 word
# hook, and the blanket constraint above would forbid the very words the graphic exists to show.
# So a hook-carrying render gets this INSTEAD — the hook named exactly, everything else still
# refused. FLUX gets it positively, for the same reason as `_NO_MARKS_FLUX`.
_HOOK_ONLY_GPT = (' The only text in the image is exactly "{hook}", set in a heavy geometric '
                  'sans-serif in sentence case — no other words, letters, numbers, logos, '
                  'watermarks or UI.')
_HOOK_ONLY_FLUX = (' The only lettering in the image is the exact phrase "{hook}", set in a heavy '
                   'geometric sans-serif in sentence case; every other garment and surface is '
                   'plain and unbranded.')

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
# The board/page/printed-surface clause — the one a hook-carrying graphic must not be handed.
_PRINTED_SURFACE_INDEX = 1


def with_no_marks(prompt: str, backend: str = "gpt-image", *,
                  scene: Optional[str] = None, hook_text: Optional[str] = None) -> str:
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
        hook_text: the editorial_graphic hook (issue #2241). When set, the blanket constraint is
            replaced by one naming that hook as the ONLY text allowed, and the printed-surface
            clause is skipped — it would blank the very surface the hook is set on.
    """
    if hook_text:
        suffix = (_HOOK_ONLY_FLUX if backend == "flux" else _HOOK_ONLY_GPT).format(hook=hook_text)
    else:
        suffix = _NO_MARKS_FLUX if backend == "flux" else _NO_MARKS_GPT
    blankets = (_NO_MARKS_GPT, _NO_MARKS_FLUX, suffix)
    marked = prompt if any(b in prompt for b in blankets) else f"{prompt}{suffix}"
    source = prompt if scene is None else scene
    for blanket in blankets:
        source = source.replace(blanket, "")
    for index, (pattern, clause) in enumerate(_MARK_MAGNET_PATTERNS):
        if hook_text and index == _PRINTED_SURFACE_INDEX:
            continue
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
                         scene: Optional[str] = None,
                         hook_text: Optional[str] = None) -> tuple[str, str]:
    """One render, plus the backend that actually produced it (``gpt-image`` or ``flux``).

    Which backend ran is not answerable from configuration: under the default ``auto`` gpt-image
    leads and FLUX silently catches its failures. The gate's repair round has to phrase itself
    for the renderer that will READ it, and a config-derived answer names the defect back at FLUX
    on exactly the runs where gpt-image is down (issue #1141).

    ``scene`` is the author's brief when ``prompt`` carries a repair round on top of it — it is
    what the mark-magnet clauses are matched against (issue #1376). ``hook_text`` is the one
    string an editorial_graphic may carry (issue #2241), handed to ``with_no_marks``.
    """
    backend = (IMAGE_BACKEND or "auto").strip().lower()
    if backend not in ("auto", "gpt-image", "flux"):
        log_warning(f"Unknown IMAGE_BACKEND '{backend}' — using auto")
        backend = "auto"

    if backend in ("auto", "gpt-image"):
        try:
            path = _render_via_gpt_image(with_no_marks(prompt, "gpt-image", scene=scene,
                                                       hook_text=hook_text),
                                         ratio=ratio, quality=quality, user_id=user_id,
                                         post_id=post_id, surface=surface)
            return conform_to_ratio(path, ratio), "gpt-image"
        except Exception as e:
            if backend == "gpt-image":
                raise
            log_warning("gpt-image render failed — falling back to FLUX", exc=e,
                        user_id=user_id, post_id=post_id, api_provider="openai", surface=surface)
    path = _render_via_flux(with_no_marks(prompt, "flux", scene=scene, hook_text=hook_text),
                            ratio=ratio,
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

# Stage 4b: the targeted questions, asked only AFTER the blind description exists.
_TARGETED_JUDGE_PROMPT = """You are grading ONE AI-generated image for a LinkedIn {surface}.
A viewer who knew nothing about its purpose described it as:
<blind>{blind}</blind>

The image was made for a piece arguing: {thesis}
The cover's headline: {headline}
Look at the image itself and answer:
1. Is each of these things visibly depicted? {entities}
2. Transcribe ALL text visible in the image. {hook_question}
3. Is any of these stock symbols present: {cliches}?
4. Does the main subject still read as a 400x225 thumbnail?
5. Any AI artifacts — waxy skin, malformed hands, melted or fused objects, garbled lettering?
6. Reading the headline together with the image, would a viewer get the GIST of the claim
   above? Not every detail or benefit — just the gist.
7. Does a face show a clear, specific emotion readable at 400x225?
8. Does the visible emotion match "{emotional_beat}"?
9. Does a gold accent or the charcoal / off-white brand palette read in the image?
10. Is the headline set in a sans-serif typeface? (true when there is no headline)
11. Could this exact image be reused unchanged on an unrelated business article?

Score each criterion 1-5, 5 best: specificity (image AND headline together convey THIS piece's
argument, with at least one of its things visible — not a generic scene), no_cliche (5 = no stock
symbol at all), thumbnail_read, text_accuracy (null when the image should carry no text and carries
none), craft (no artifacts), scroll_stop (would it stop a scroll — a readable emotion counts most),
brand_fit (the brand palette reads).
Respond with ONLY a JSON object:
{{"entities_depicted": {{"<thing>": true}}, "text_seen": "<exact transcription, or empty>",
 "cliches_present": ["..."], "thesis_inferable": true, "face_emotion": true,
 "emotion_matches": true, "headline_sans_serif": true, "reusable_elsewhere": false,
 "rubric": {{"specificity": 1, "no_cliche": 1, "thumbnail_read": 1, "text_accuracy": null,
            "craft": 1, "scroll_stop": 1, "brand_fit": 1}},
 "issues": ["<short actionable phrase>"]}}"""

RUBRIC_CRITERIA = ("specificity", "no_cliche", "thumbnail_read", "text_accuracy", "craft",
                   "scroll_stop", "brand_fit")
# Acceptable iff every floor holds. `text_accuracy` None means "no text expected, none seen";
# `brand_fit` None means the judge did not answer it (never a fail on its own).
_RUBRIC_FLOORS = {"specificity": 4, "no_cliche": 5, "text_accuracy": 4, "craft": 4,
                  "brand_fit": 3}
# Round 3 (#2241): four rejected covers were people at laptops with neutral faces, scroll_stop 3.
# On the surfaces that have to stop a feed, 3 is a fail.
_SCROLL_STOP_SURFACES = frozenset({"newsletter", "post_image"})
_SCROLL_STOP_FLOOR = 4
_REQUIRED_SCORES = ("specificity", "no_cliche", "craft")


def _score(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(1, min(5, int(value)))


def _normalise_text(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split())


def _no_text_seen(text: str) -> bool:
    normal = _normalise_text(text)
    return not normal or normal in ("none", "no text", "no visible text", "n a", "na")


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


def stray_texts(blind: str, hook_text: Optional[str]) -> list[str]:
    """Text the blind description saw that is not (part of) the declared hook.

    Gauntlet round 1 of #2241: the blind judge transcribed '$30K' on a tag beside the hook, and a
    garbled report title, and text_accuracy still scored 5 — the targeted judge reported only the
    hook. This reads the BLIND transcription deterministically instead.

    Args:
        blind: The blind judge's description.
        hook_text: The one string the image may carry, or None.

    Returns:
        Each stray string; empty when every transcribed string is contained in the hook.
    """
    hook = _normalise_text(hook_text or "")
    return [t for t in blind_text_strings(blind)
            if not hook or _normalise_text(t) not in hook]


def _apply_overlays(rubric: dict, *, blind: str, answer: dict, entities: list,
                    hook_text: Optional[str], face_expected: bool = False) -> dict:
    """Deterministic corrections the judge cannot talk its way past.

    A stock symbol the BLIND description names, or the judge itself lists, caps ``no_cliche``; a
    transcription that is not the hook caps ``text_accuracy``, and ANY text the blind description
    quotes beyond the hook caps it at 2. ``specificity`` is judged on the headline AND image
    together (round 3): no anchor seen at all, or a thesis the judge says the pair does not
    convey, caps it at 3 — the anchor count is otherwise advisory. Where a face is expected (a
    people_scene), no clear emotion readable at thumbnail size caps ``scroll_stop`` at 3.
    """
    from cqc_lem.utilities.ai.image_brief import cliche_hit

    rubric = dict(rubric)
    cliches = [str(c) for c in (answer.get("cliches_present") or []) if str(c).strip()]
    if cliches or cliche_hit(blind):
        rubric["no_cliche"] = min(rubric.get("no_cliche") or 5, 2)
    text_seen = str(answer.get("text_seen") or "")
    if hook_text:
        if _normalise_text(text_seen) != _normalise_text(hook_text):
            rubric["text_accuracy"] = min(rubric.get("text_accuracy") or 3, 3)
    elif not _no_text_seen(text_seen):
        rubric["text_accuracy"] = min(rubric.get("text_accuracy") or 2, 2)
    if stray_texts(blind, hook_text):
        rubric["text_accuracy"] = min(rubric.get("text_accuracy") or 2, 2)
    seen = answer.get("entities_depicted") or {}
    if entities and isinstance(seen, dict) and not any(seen.get(e) is True for e in entities):
        rubric["specificity"] = min(rubric.get("specificity") or 3, 3)
    if answer.get("thesis_inferable") is False:
        rubric["specificity"] = min(rubric.get("specificity") or 3, 3)
    if face_expected and answer.get("face_emotion") is False:
        rubric["scroll_stop"] = min(rubric.get("scroll_stop") or 3, 3)
    # Round 4 (#2241): ed16's broad smile only loosely matched "relief"; a mismatch is a fail.
    if answer.get("emotion_matches") is False:
        rubric["scroll_stop"] = min(rubric.get("scroll_stop") or 3, 3)
    # ed16's hook rendered in a serif face — off brand.
    if hook_text and answer.get("headline_sans_serif") is False:
        rubric["brand_fit"] = min(rubric.get("brand_fit") or 3, 3)
    # ed18's generic flip-chart presentation scored specificity 5: an image that would fit any
    # business article is not specific to this one, whatever else the judge says.
    if answer.get("reusable_elsewhere") is True:
        rubric["specificity"] = min(rubric.get("specificity") or 3, 3)
    return rubric


def _staged_inspect(image_path: str, concept: Any, hook_text: Optional[str],
                    surface: Optional[str]) -> QualityVerdict:
    """Stage 4: a BLIND description first, then targeted questions graded on a rubric."""
    from cqc_lem.utilities.ai.image_brief import CLICHE_OBJECTS, usable_anchors

    try:
        image_part = _image_part(image_path)
        blind_response = client.chat.completions.create(
            model="lem-vision",
            messages=[{"role": "user", "content": [
                {"type": "text", "text": BLIND_JUDGE_PROMPT}, image_part]}],
            temperature=0,
            max_tokens=350,
        )
        blind = " ".join(str(blind_response.choices[0].message.content or "").split())[:1200]
        entities = usable_anchors(concept)
        hook_question = (f'Does it equal exactly "{hook_text}"?' if hook_text
                         else "This image should carry no text at all.")
        headline = f'"{hook_text}"' if hook_text else "(none — judge the image alone)"
        targeted = client.chat.completions.create(
            model="lem-vision",
            messages=[{"role": "user", "content": [
                {"type": "text", "text": _TARGETED_JUDGE_PROMPT.format(
                    surface=surface or "post", blind=blind or "(no description)",
                    thesis=concept.thesis, headline=headline,
                    emotional_beat=getattr(concept, "emotional_beat", "") or "the piece's mood",
                    entities="; ".join(entities) or "(none named)",
                    hook_question=hook_question, cliches=", ".join(CLICHE_OBJECTS))},
                image_part]}],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=500,
        )
        answer = _judge_json(targeted, "targeted judge")
        raw_rubric = answer.get("rubric") or {}
        rubric = {name: _score(raw_rubric.get(name)) for name in RUBRIC_CRITERIA}
        if any(rubric[name] is None for name in _REQUIRED_SCORES):
            raise ValueError("rubric is missing a required score")
        rubric = _apply_overlays(rubric, blind=blind, answer=answer, entities=entities,
                                 hook_text=hook_text,
                                 face_expected=getattr(concept, "treatment", "") == "people_scene")
    except Exception as e:
        return _unchecked(e, surface, "staged")

    floors = dict(_RUBRIC_FLOORS)
    if surface in _SCROLL_STOP_SURFACES:
        floors["scroll_stop"] = _SCROLL_STOP_FLOOR
    failing = [name for name, floor in floors.items()
               if rubric.get(name) is not None and rubric[name] < floor]
    issues = [f"{name} {rubric[name]}/5" for name in failing]
    issues += [f"stray text: {t}" for t in stray_texts(blind, hook_text)][:3]
    issues += [str(i) for i in (answer.get("issues") or []) if str(i).strip()]
    return QualityVerdict(acceptable=not failing, relevance=rubric.get("specificity"),
                          issues=issues[:8], rubric=rubric, blind_description=blind,
                          failing=failing)


def inspect_render_quality(image_path: str, focal_concept: str,
                           surface: Optional[str] = None, *,
                           concept: Any = None,
                           hook_text: Optional[str] = None) -> QualityVerdict:
    """Vision look at a finished render. Fails OPEN — any error returns acceptable/unchecked.

    With a Stage 1 ``concept`` (issue #2241) the gate is two ``lem-vision`` calls: a BLIND
    description that is shown nothing about the brief or the piece, then targeted questions —
    per-entity presence, an exact transcription against ``hook_text``, the stock-symbol list,
    thumbnail legibility, artifacts, and whether the thesis reads — scored on ``RUBRIC_CRITERIA``.
    Acceptable iff specificity >= 4, no_cliche == 5, craft >= 4 and text_accuracy >= 4 (or n/a).
    Grading against the brief's own focal concept was circular: a valve scored 5 against "a valve
    symbolising leaks".

    Without a concept the legacy single call runs: ``surface`` turns on the newsletter-only
    stock-office rule, and for ``newsletter``/``post_image`` (issue #2015) raises the relevance
    floor to ``_STRICT_MIN_RELEVANCE``.

    Args:
        image_path: The finished render on disk.
        focal_concept: The brief's focal concept — the legacy gate's only yardstick.
        surface: The surface the render is for.
        concept: Stage 1's ``ImageConcept``; switches on the staged judge.
        hook_text: The one string an editorial_graphic may carry.

    Returns:
        The verdict; ``checked=False`` when the gate could not run.
    """
    if concept is None:
        return _legacy_inspect(image_path, focal_concept, surface)
    return _staged_inspect(image_path, concept, hook_text, surface)


_RUBRIC_REPAIRS_GPT = {
    "specificity": "show {entities} literally, so the image is plainly about {thesis}",
    "no_cliche": "remove every stock symbol ({cliches}) and build the frame on {entities}",
    "text_accuracy": "{text_fix}",
    "craft": "natural skin texture, hands relaxed or out of frame, every object whole and solid",
    "thumbnail_read": "make the subject larger and simpler, centred in the frame",
    "scroll_stop": ("a closer framing where {emotion} shows on the face as a specific reaction, "
                    "a laptop at most a prop"),
    "brand_fit": "one deliberate warm gold accent against the charcoal and off-white palette",
}
# FLUX renders what a prompt NAMES, so its repair never names the defect — only what to show.
_RUBRIC_REPAIRS_FLUX = {
    "specificity": "{entities} shown literally, so the image is plainly about {thesis}",
    "no_cliche": "the whole frame built on {entities}",
    "text_accuracy": "{text_fix}",
    "craft": "natural skin texture, hands relaxed and out of frame, every object whole and solid",
    "thumbnail_read": "the subject larger and simpler, centred in the frame",
    "scroll_stop": "a closer framing where {emotion} shows on the face as a specific reaction",
    "brand_fit": "one deliberate warm gold accent against a charcoal and off-white palette",
}


def rubric_repair_directive(verdict: QualityVerdict, backend: str, concept: Any,
                            hook_text: Optional[str]) -> str:
    """The re-render clause for a staged-judge rejection, built from its FAILING criteria.

    Args:
        verdict: The staged verdict; ``failing`` names the criteria below their floor.
        backend: ``flux`` or ``gpt-image``; FLUX is only ever told what to SHOW.
        concept: Stage 1's concept, whose entities and thesis the repair names back.
        hook_text: The graphic's hook, when the text criterion failed on one.

    Returns:
        One directive sentence for the next attempt.
    """
    from cqc_lem.utilities.ai.image_brief import usable_anchors

    entities = "; ".join(usable_anchors(concept)[:3]) or "the piece's own specific subject"
    thesis = getattr(concept, "thesis", "") or "the piece's idea"
    if hook_text:
        text_fix = (f'the only text spelled exactly "{hook_text}"' if backend == "flux" else
                    f'make the only text exactly "{hook_text}", spelled correctly')
    else:
        text_fix = "every garment and surface plain and unmarked, screens blank, walls clean"
    if backend != "flux":
        # Round 4: name the fix for stray marks outright — papers and screens are where they live.
        text_fix = ("remove every legible mark from papers and screens; the only text is the "
                    "hook" + (f' "{hook_text}", spelled exactly' if hook_text else " (there is none)"))
    cliches = ", ".join(i for i in verdict.issues if "/5" not in i)[:120] or "generic symbols"
    table = _RUBRIC_REPAIRS_FLUX if backend == "flux" else _RUBRIC_REPAIRS_GPT
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


def _gate_loop(render_once, *, prompt: str, surface: str, focal_concept: Optional[str],
               concept: Any, hook_text: Optional[str], user_id: Optional[int],
               post_id: Optional[int], render_info: Optional[dict],
               log_message: str, enforce: Optional[bool] = None) -> Optional[str]:
    """The bounded render → judge → repair loop both gated renderers share.

    ``render_once(current_prompt)`` returns ``(path, backend, info)`` — ``path`` None means the
    render produced nothing and the loop returns None at once; ``info`` is merged into
    ``render_info`` for the candidate actually returned. ``enforce`` overrides the surface's
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
    gate_kwargs = {"concept": concept, "hook_text": hook_text} if concept is not None else {}

    for attempt in range(1, attempts + 1):
        best: Optional[tuple] = None
        for _ in range(candidates):
            cand_path, backend, info = render_once(current_prompt)
            if not cand_path:
                return None
            verdict = inspect_render_quality(cand_path, focal_concept or prompt[:200],
                                             surface=surface, **gate_kwargs)
            if not verdict.checked and enforced:
                # ONE more look before failing open on a surface the gate is meant to hold: a
                # judge blip must not wave an ungraded render through (#2249 gauntlet).
                verdict = inspect_render_quality(cand_path, focal_concept or prompt[:200],
                                                 surface=surface, **gate_kwargs)
            if best is None or _verdict_rank(verdict) > _verdict_rank(best[2]):
                best = (cand_path, backend, verdict, info)
            if verdict.acceptable or not verdict.checked:
                break
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
        directive = (rubric_repair_directive(verdict, used_backend, concept, hook_text)
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
                              enforce: Optional[bool] = None) -> Optional[str]:
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

    ``concept``/``hook_text`` (issue #2241) switch on the staged blind judge, exactly as for
    ``render_image_gated``.
    """
    from cqc_lem.utilities.ai.ai_helper import _record_avatar_media
    from cqc_lem.utilities.avatar.attributes import apply_subject_clause
    from cqc_lem.utilities.avatar.replicate_avatar import generate_image_with_avatar

    def render_once(current_prompt: str) -> tuple:
        # The likeness path talks to Replicate directly, so it never passes through
        # render_image_from_prompt where the constraint is otherwise added. Always FLUX.
        marked = with_no_marks(current_prompt, "flux", scene=prompt, hook_text=hook_text)
        path, used_avatar = generate_image_with_avatar(
            apply_subject_clause(marked, avatar), avatar["model_ref"],
            ratio=ratio, fallback_prompt=marked, surface=surface)
        path = conform_to_ratio(path, ratio)
        if used_avatar and path:
            # Provenance for a synthetic likeness of a real person.
            _record_avatar_media(path, post_id, user_id)
        if not path and render_info is not None:
            render_info["used_avatar"] = bool(used_avatar)
        return path, "flux", {"used_avatar": bool(used_avatar)}

    return _gate_loop(render_once, prompt=prompt, surface=surface, focal_concept=focal_concept,
                      concept=concept, hook_text=hook_text, user_id=user_id, post_id=post_id,
                      render_info=render_info, log_message="Avatar image failed the quality gate",
                      enforce=enforce)


def render_image_gated(prompt: str, *, surface: str, ratio: str = "1:1",
                       focal_concept: Optional[str] = None,
                       quality: Optional[str] = None,
                       user_id: Optional[int] = None,
                       post_id: Optional[int] = None,
                       image_model: str = DEFAULT_IMAGE_MODEL,
                       render_info: Optional[dict] = None,
                       concept: Any = None,
                       hook_text: Optional[str] = None,
                       enforce: Optional[bool] = None) -> str:
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
    two candidates per attempt and keeps the better. ``hook_text`` is the one string an
    editorial_graphic may carry, handed to the renderer's no-marks clause and to the judge.
    """
    def render_once(current_prompt: str) -> tuple:
        # The backend that RENDERED, not the one configured: under `auto` a gpt-image failure
        # falls through to FLUX, and the retry has to be phrased for whichever one answered.
        # `scene=prompt`: from attempt 2 `current_prompt` carries the repair round too, and the
        # mark-magnet clauses must stay derived from what the AUTHOR described (issue #1376).
        path, backend = _render_with_backend(current_prompt, ratio=ratio, quality=quality,
                                             user_id=user_id, post_id=post_id,
                                             image_model=image_model, surface=surface,
                                             scene=prompt, hook_text=hook_text)
        return path, backend, {}

    return _gate_loop(render_once, prompt=prompt, surface=surface, focal_concept=focal_concept,
                      concept=concept, hook_text=hook_text, user_id=user_id, post_id=post_id,
                      render_info=render_info, log_message="Image failed the quality gate",
                      enforce=enforce)
