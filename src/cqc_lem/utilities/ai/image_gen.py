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
import json
import os
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Optional

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

# gpt-image accepts exactly these; anything else falls back to square.
_SIZE_BY_RATIO = {
    "1:1": "1024x1024",
    "16:9": "1536x1024",
    "9:16": "1024x1536",
}

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


def with_no_marks(prompt: str, backend: str = "gpt-image") -> str:
    """The render prompt plus the no-marks constraint for that backend, added at most once.

    A prompt that NAMES a mark-carrying surface — a screen, a whiteboard, a page — also gets that
    surface's blank-state clause, because the blanket constraint measurably did not hold there
    (issue #1376). The match runs against the SCENE, with either blanket constraint stripped back
    out first: `_NO_MARKS_FLUX` itself says "screens are blank", so matching the whole string would
    make every FLUX render look like it described a screen.
    """
    suffix = _NO_MARKS_FLUX if backend == "flux" else _NO_MARKS_GPT
    if _NO_MARKS_GPT in prompt or _NO_MARKS_FLUX in prompt:
        marked = prompt
    else:
        marked = f"{prompt}{suffix}"
    scene = prompt.replace(_NO_MARKS_GPT, "").replace(_NO_MARKS_FLUX, "")
    for pattern, clause in _MARK_MAGNET_PATTERNS:
        if clause not in marked and pattern.search(scene):
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


def size_for_ratio(ratio: str) -> str:
    """The gpt-image `size` for an aspect ratio; anything unrecognised falls back to square.

    Only the three ratios the surfaces render at map to a size the API accepts, so an unknown ratio
    degrades to 1024x1024 rather than being passed through as a size the request would fail on.
    """
    return _SIZE_BY_RATIO.get(ratio, "1024x1024")


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


def run_replicate_bounded(ref: str, input_params: dict,
                          timeout_s: int = _REPLICATE_TIMEOUT_SECONDS,
                          attempts: int = _REPLICATE_ATTEMPTS):
    """``replicate.run`` with a hard timeout and bounded retry + backoff.

    The bare call blocks until the prediction resolves — a stuck one used to hold a worker
    slot indefinitely, and any transient 5xx was terminal.
    """
    import replicate

    last_error: Exception = RuntimeError("replicate.run was never attempted")
    for attempt in range(1, max(1, attempts) + 1):
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(replicate.run, ref, input=input_params)
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
                         surface: Optional[str] = None) -> tuple[str, str]:
    """One render, plus the backend that actually produced it (``gpt-image`` or ``flux``).

    Which backend ran is not answerable from configuration: under the default ``auto`` gpt-image
    leads and FLUX silently catches its failures. The gate's repair round has to phrase itself
    for the renderer that will READ it, and a config-derived answer names the defect back at FLUX
    on exactly the runs where gpt-image is down (issue #1141).
    """
    backend = (IMAGE_BACKEND or "auto").strip().lower()
    if backend not in ("auto", "gpt-image", "flux"):
        log_warning(f"Unknown IMAGE_BACKEND '{backend}' — using auto")
        backend = "auto"

    if backend in ("auto", "gpt-image"):
        try:
            return _render_via_gpt_image(with_no_marks(prompt, "gpt-image"), ratio=ratio,
                                         quality=quality, user_id=user_id,
                                         post_id=post_id, surface=surface), "gpt-image"
        except Exception as e:
            if backend == "gpt-image":
                raise
            log_warning("gpt-image render failed — falling back to FLUX", exc=e,
                        user_id=user_id, post_id=post_id, api_provider="openai", surface=surface)
    return _render_via_flux(with_no_marks(prompt, "flux"), ratio=ratio,
                            image_model=image_model, user_id=user_id, surface=surface), "flux"


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
Each issue string must be a short actionable phrase (e.g. "garbled text on whiteboard")."""

# The gate is asked whether the render carries marks, so it has to be able to READ one. At
# `low` the API downsamples to ~512px on the long edge, where four logo tiles on a laptop screen
# in a 1536x1024 cover are simply not resolvable — which is how ed9 passed this gate carrying the
# exact thing it is asked about (issue #1376). A high-detail read costs ~1k image tokens on
# lem-vision against an image render that costs an order of magnitude more, so the gate's own
# question is worth answering properly.
_VISION_GATE_DETAIL = "high"


def inspect_render_quality(image_path: str, focal_concept: str) -> QualityVerdict:
    """Vision look at a finished render. Fails OPEN — any error returns acceptable/unchecked."""
    try:
        with open(image_path, "rb") as fh:
            encoded = base64.b64encode(fh.read()).decode("ascii")
        ext = os.path.splitext(image_path)[1].lstrip(".").lower() or "png"
        mime = "jpeg" if ext in ("jpg", "jpeg") else ext
        response = client.chat.completions.create(
            model="lem-vision",
            messages=[{"role": "user", "content": [
                {"type": "text",
                 "text": _VISION_GATE_PROMPT.format(focal=(focal_concept or "professional "
                                                           "LinkedIn content")[:400])},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/{mime};base64,{encoded}",
                               "detail": _VISION_GATE_DETAIL}},
            ]}],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=300,
        )
        verdict = json.loads(response.choices[0].message.content)
        issues = verdict.get("issues") or []
        return QualityVerdict(acceptable=bool(verdict.get("acceptable")),
                              relevance=verdict.get("relevance"),
                              issues=[str(i) for i in issues][:6])
    except Exception as e:
        log_debug("Image quality gate unavailable — passing render through", error=str(e))
        return QualityVerdict(acceptable=True, checked=False)


def render_avatar_image_gated(prompt: str, *, avatar: dict, user_id: Optional[int],
                              surface: str, ratio: str = "1:1",
                              focal_concept: Optional[str] = None,
                              post_id: Optional[int] = None,
                              render_info: Optional[dict] = None) -> Optional[str]:
    """LoRA render of an image the author appears in, behind the SAME bounded vision gate the
    base renderer gets.

    Likeness never renders through the gpt-image path (``generate_post_image`` owns the avatar
    guardrails), so without this the avatar branch was the one path with no quality check at all —
    which is how a post about LLM routing costs came back as a plain headshot against a brick wall.
    Returns the best candidate's path, or None if nothing rendered.

    ``render_info``, when passed, is filled with ``{"used_avatar": bool}`` for the render actually
    RETURNED (issue #1430). ``posts.avatar_media`` cannot answer that question: it is a sticky
    per-POST flag that any earlier avatar render sets and nothing clears, so on a re-render or a
    gate retry that fell back to base Flux it still reads true — which would file a fallback
    frame's likeness verdict under the LoRA render it is meant to be separated from. It also
    carries ``gate_verdict`` for the same reason: the verdict is only in hand here, and the brief
    receipt stored beside the render (issue #1377) is what makes it auditable afterwards.
    """
    from cqc_lem.utilities.ai.ai_helper import _record_avatar_media
    from cqc_lem.utilities.avatar.attributes import apply_subject_clause
    from cqc_lem.utilities.avatar.replicate_avatar import generate_image_with_avatar
    from cqc_lem.utilities.observability import track_image_gate_verdict

    enforced = surface in IMAGE_QUALITY_GATE_SURFACES
    attempts = max(1, IMAGE_GATE_MAX_ATTEMPTS) if enforced else 1
    current_prompt = prompt
    path: Optional[str] = None
    last_verdict: Optional[QualityVerdict] = None

    for attempt in range(1, attempts + 1):
        # The likeness path talks to Replicate directly, so it never passes through
        # render_image_from_prompt where the constraint is otherwise added. Always FLUX.
        marked = with_no_marks(current_prompt, "flux")
        path, used_avatar = generate_image_with_avatar(
            apply_subject_clause(marked, avatar), avatar["model_ref"],
            ratio=ratio, fallback_prompt=marked, surface=surface)
        if render_info is not None:
            # Per ATTEMPT: the last one written is the render this call returns.
            render_info["used_avatar"] = bool(used_avatar)
        if used_avatar and path:
            # Provenance for a synthetic likeness of a real person.
            _record_avatar_media(path, post_id, user_id)
        if not path:
            return None
        verdict = inspect_render_quality(path, focal_concept or prompt[:200])
        last_verdict = verdict
        if verdict.acceptable or not verdict.checked:
            break
        log_info("Avatar image failed the quality gate", user_id=user_id, post_id=post_id,
                 action_type="image_gate", surface=surface, attempt=attempt,
                 issues="; ".join(verdict.issues))
        if not enforced or attempt == attempts:
            break
        # Always FLUX on this path — the likeness renders on Replicate.
        current_prompt = f"{prompt}\n\n{repair_directive(verdict.issues, 'flux', focal_concept)}"

    if last_verdict is not None:
        if last_verdict.checked:
            gate_verdict = "accepted" if last_verdict.acceptable else "rejected"
        else:
            gate_verdict = "unchecked"
        if render_info is not None:
            render_info["gate_verdict"] = gate_verdict
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


def render_image_gated(prompt: str, *, surface: str, ratio: str = "1:1",
                       focal_concept: Optional[str] = None,
                       quality: Optional[str] = None,
                       user_id: Optional[int] = None,
                       post_id: Optional[int] = None,
                       image_model: str = DEFAULT_IMAGE_MODEL,
                       render_info: Optional[dict] = None) -> str:
    """Render with the bounded vision gate. Returns the best candidate's path.

    Surfaces outside IMAGE_QUALITY_GATE_SURFACES get one advisory-only pass (verdict logged,
    render kept). After the attempt budget, the last render ships — for covers the human
    review queue is still behind this gate.

    ``render_info``, when passed, is filled with ``{"gate_verdict": "accepted"|"rejected"|
    "unchecked"}`` — the same out-param shape the avatar renderer uses. The verdict exists only
    inside this call, and recording it beside the stored render (issue #1377) is what lets a later
    audit tell a render the gate passed from one it never looked at.
    """
    from cqc_lem.utilities.observability import track_image_gate_verdict

    enforced = surface in IMAGE_QUALITY_GATE_SURFACES
    attempts = max(1, IMAGE_GATE_MAX_ATTEMPTS) if enforced else 1
    current_prompt = prompt
    path: Optional[str] = None
    last_verdict: Optional[QualityVerdict] = None

    for attempt in range(1, attempts + 1):
        # The backend that RENDERED, not the one configured: under `auto` a gpt-image failure
        # falls through to FLUX, and the retry has to be phrased for whichever one answered.
        path, used_backend = _render_with_backend(current_prompt, ratio=ratio, quality=quality,
                                                 user_id=user_id, post_id=post_id,
                                                 image_model=image_model, surface=surface)
        verdict = inspect_render_quality(path, focal_concept or prompt[:200])
        last_verdict = verdict
        if verdict.acceptable or not verdict.checked:
            break
        log_info("Image failed the quality gate",
                 user_id=user_id, post_id=post_id, action_type="image_gate",
                 surface=surface, attempt=attempt, issues="; ".join(verdict.issues))
        if not enforced or attempt == attempts:
            break
        current_prompt = f"{prompt}\n\n{repair_directive(verdict.issues, used_backend, focal_concept)}"

    if last_verdict is not None:
        if last_verdict.checked:
            gate_verdict = "accepted" if last_verdict.acceptable else "rejected"
        else:
            gate_verdict = "unchecked"
        if render_info is not None:
            render_info["gate_verdict"] = gate_verdict
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
