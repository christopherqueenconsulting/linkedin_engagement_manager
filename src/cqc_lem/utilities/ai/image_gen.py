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
# Round 9 (#2241): gpt-image painted the brief's role and group nouns as labels — a poster
# reading "Founders & Content Teams In Conversation", badges saying "SECURITY" — so both
# backends now refuse every label-carrying surface by name.
_NO_LABELS = (" No captions, titles, posters, signage, name badges, lanyards with text, or labels "
              "of any kind.")
_NO_MARKS_GPT = (" Absolutely no text, letters, words, numbers, captions, watermarks, logos, "
                 "brand marks, app icons, social-media icons, charts, or UI elements anywhere "
                 "in the image." + _NO_LABELS)
_NO_MARKS_FLUX = (" Every garment and surface is plain and unbranded, screens are blank, walls "
                  "clean and unmarked." + _NO_LABELS)

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
            return _render_via_gpt_image(with_no_marks(prompt, "gpt-image", scene=scene),
                                         ratio=ratio, quality=quality, user_id=user_id,
                                         post_id=post_id, surface=surface), "gpt-image"
        except Exception as e:
            if backend == "gpt-image":
                raise
            log_warning("gpt-image render failed — falling back to FLUX", exc=e,
                        user_id=user_id, post_id=post_id, api_provider="openai", surface=surface)
    return _render_via_flux(with_no_marks(prompt, "flux", scene=scene), ratio=ratio,
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
        verdict = json.loads(response.choices[0].message.content)
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
        log_debug("Image quality gate unavailable — passing render through", error=str(e))
        return QualityVerdict(acceptable=True, checked=False)


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

Score each criterion 1-5, 5 best:
- specificity: 5 = with its headline, a scroller would correctly guess this piece's argument —
  the kicker + headline name the exact topic and the scene shows the human stakes of it (a
  generic office is fine when kicker, headline and emotion are specific; a 5 needs all three
  working together); 4 = clearly on-topic, slightly generic scene; 3 = the scene could sit on many unrelated posts
  even with the headline; 2 or less = misleading or off-topic.
- no_cliche (5 = no stock symbol at all), thumbnail_read, craft (no artifacts, an authentic
  face), scroll_stop (would it stop a scroll — a readable, fitting emotion counts most), brand_fit
  (the brand palette reads).
Respond with ONLY a JSON object:
{{"entities_depicted": {{"<thing>": true}}, "cliches_present": ["..."],
 "thesis_inferable": true, "face_emotion": true, "emotion_matches": true,
 "emotion_authentic": true, "headline_names_subject": true, "bright_enough": true,
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
_SCROLL_STOP_FLOOR = 4
_REQUIRED_SCORES = ("specificity", "no_cliche", "craft")
# The surfaces whose headline ``image_compose`` typesets onto the render.
COMPOSE_SURFACES = frozenset({"newsletter", "post_image"})


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
            if not hook or _normalise_text(t) not in hook]


def _apply_overlays(rubric: dict, *, blind: str, answer: dict, entities: list,
                    hook_text: Optional[str], face_expected: bool = False) -> dict:
    """Deterministic corrections the judge cannot talk its way past.

    ``text_accuracy`` is DECIDED here: any text the blind look found on the raw render is a 2,
    none is n/a. A stock symbol the blind description names, or the judge lists, caps
    ``no_cliche``. No anchor seen, or a gist the headline + image do not convey, caps
    ``specificity`` at 3; a headline that never names its subject caps it at 4. A missing or
    mismatched emotion caps ``scroll_stop``, a cartoonish one caps ``craft``, a dark image caps
    ``thumbnail_read``.
    """
    from cqc_lem.utilities.ai.image_brief import cliche_hit

    rubric = dict(rubric)
    cliches = [str(c) for c in (answer.get("cliches_present") or []) if str(c).strip()]
    if cliches or cliche_hit(blind):
        rubric["no_cliche"] = min(rubric.get("no_cliche") or 5, 2)
    rubric["text_accuracy"] = 2 if stray_texts(blind) else None
    # Round 8: specificity is the judge's ANCHORED DESCRIPTORS on kicker + headline + scene
    # together — nothing deterministic caps it. Anchors seen, the gist and a subject-less
    # headline are advisory issues only (``advisory_issues``): gpt-4.1's "no vendor contract
    # depicted" was asking for exactly the props the brief now refuses to draw.
    if face_expected and answer.get("face_emotion") is False:
        rubric["scroll_stop"] = min(rubric.get("scroll_stop") or 3, 3)
    if answer.get("emotion_matches") is False:
        rubric["scroll_stop"] = min(rubric.get("scroll_stop") or 3, 3)
    # Round 6: a wailing man over a bill, caricature faces — a fake emotion is a craft defect.
    if answer.get("emotion_authentic") is False:
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
                    cliches=", ".join(CLICHE_OBJECTS))},
                _image_part(composite_path) if composite_path else raw_part]}],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=500,
        )
        answer = json.loads(targeted.choices[0].message.content)
        raw_rubric = answer.get("rubric") or {}
        rubric = {name: _score(raw_rubric.get(name)) for name in RUBRIC_CRITERIA}
        if any(rubric[name] is None for name in _REQUIRED_SCORES):
            raise ValueError("rubric is missing a required score")
        rubric = _apply_overlays(rubric, blind=blind, answer=answer, entities=entities,
                                 hook_text=hook_text,
                                 face_expected=getattr(concept, "treatment", "") == "people_scene")
    except Exception as e:
        log_debug("Staged image judge unavailable — passing render through", error=str(e),
                  surface=surface, action_type="image_gate")
        return QualityVerdict(acceptable=True, checked=False)

    floors = dict(_RUBRIC_FLOORS)
    if surface in _SCROLL_STOP_SURFACES:
        floors["scroll_stop"] = _SCROLL_STOP_FLOOR
        floors["thumbnail_read"] = _SCROLL_STOP_FLOOR
    failing = [name for name, floor in floors.items()
               if rubric.get(name) is not None and rubric[name] < floor]
    issues = [f"{name} {rubric[name]}/5" for name in failing]
    issues += [f"stray text: {t}" for t in stray_texts(blind)][:3]
    issues += advisory_issues(answer, entities, hook_text)
    issues += [str(i) for i in (answer.get("issues") or []) if str(i).strip()]
    emotion_weak = (answer.get("face_emotion") is False or answer.get("emotion_matches") is False
                    or "scroll_stop" in failing
                    or any(_EMOTION_ISSUE.search(str(i)) for i in (answer.get("issues") or [])))
    return QualityVerdict(acceptable=not failing, relevance=rubric.get("specificity"),
                          issues=issues[:8], rubric=rubric, blind_description=blind,
                          failing=failing, emotion_weak=bool(emotion_weak))


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


def _kicker_for(concept: Any) -> str:
    """Every composite carries a kicker: Stage 1's, else one derived from the concept (round 9)."""
    from cqc_lem.utilities.ai.image_concept import concept_kicker

    return concept_kicker(concept)


def _composite(raw_path: str, hook_text: Optional[str], surface: str, layout: Optional[str],
               brand_kit: Optional[str], kicker: Optional[str] = None,
               signature: Optional[str] = None) -> Optional[str]:
    """The render with its headline typeset on, or None when there is no headline or it fails.

    A compositing failure is a warning, never a lost render: the raw image still ships.
    """
    if not hook_text or surface not in COMPOSE_SURFACES:
        return None
    from cqc_lem.utilities.ai.image_compose import brand_style, compose_headline

    try:
        return compose_headline(raw_path, hook_text, layout=layout,
                                brand=brand_style(brand_kit), surface=surface, kicker=kicker,
                                signature=signature)
    except Exception as e:
        log_warning("Headline compositing failed — shipping the render without it", exc=e,
                    surface=surface, action_type="image_compose")
        return None


def _gate_loop(render_once, *, prompt: str, surface: str, focal_concept: Optional[str],
               concept: Any, hook_text: Optional[str], user_id: Optional[int],
               post_id: Optional[int], render_info: Optional[dict],
               log_message: str, layout: Optional[str] = None,
               brand_kit: Optional[str] = None,
               signature: Optional[str] = None) -> Optional[str]:
    """The bounded render → composite → judge → repair loop both gated renderers share.

    ``render_once(current_prompt)`` returns ``(path, backend, info)`` — ``path`` None means the
    render produced nothing and the loop returns None at once; ``info`` is merged into
    ``render_info`` for the candidate actually returned. On covers and posts the headline is
    composited onto each candidate (``image_compose``); the loop returns the COMPOSITE and records
    the raw render as ``render_info["raw_render_path"]``.
    """
    from cqc_lem.utilities.observability import track_image_gate_verdict

    enforced = surface in IMAGE_QUALITY_GATE_SURFACES
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
                                   signature=signature)
            gate_kwargs = ({"concept": concept, "hook_text": hook_text,
                            "composite_path": composite} if concept is not None else {})
            verdict = inspect_render_quality(cand_path, focal_concept or prompt[:200],
                                             surface=surface, **gate_kwargs)
            info = dict(info, raw_render_path=cand_path) if composite else info
            if best is None or _verdict_rank(verdict) > _verdict_rank(best[2]):
                best = (composite or cand_path, backend, verdict, info)
            if verdict.acceptable or not verdict.checked:
                break
            if verdict.emotion_weak and concept is not None and not emotion_boost:
                emotion_boost = EMOTION_DIRECTIVE.format(
                    beat=getattr(concept, "emotional_beat", "") or "the piece's emotion")
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
        if render_info is not None:
            render_info["gate_verdict"] = gate_verdict
            if last_verdict.rubric:
                # The WHY behind a rejection, for the brief receipt (issue #2241).
                render_info["gate_rubric"] = dict(last_verdict.rubric)
                render_info["gate_failing"] = list(last_verdict.failing)
                render_info["gate_issues"] = list(last_verdict.issues)
                render_info["gate_blind_description"] = last_verdict.blind_description
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
                              signature: Optional[str] = None) -> Optional[str]:
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
    judge and the composited headline, exactly as for ``render_image_gated``.
    """
    from cqc_lem.utilities.ai.ai_helper import _record_avatar_media
    from cqc_lem.utilities.avatar.attributes import apply_subject_clause
    from cqc_lem.utilities.avatar.replicate_avatar import generate_image_with_avatar

    def render_once(current_prompt: str) -> tuple:
        # The likeness path talks to Replicate directly, so it never passes through
        # render_image_from_prompt where the constraint is otherwise added. Always FLUX.
        marked = with_no_marks(current_prompt, "flux", scene=prompt)
        path, used_avatar = generate_image_with_avatar(
            apply_subject_clause(marked, avatar), avatar["model_ref"],
            ratio=_scene_ratio(ratio, hook_text, surface), fallback_prompt=marked,
            surface=surface)
        if used_avatar and path:
            # Provenance for a synthetic likeness of a real person.
            _record_avatar_media(path, post_id, user_id)
        if not path and render_info is not None:
            render_info["used_avatar"] = bool(used_avatar)
        return path, "flux", {"used_avatar": bool(used_avatar)}

    return _gate_loop(render_once, prompt=prompt, surface=surface, focal_concept=focal_concept,
                      concept=concept, hook_text=hook_text, user_id=user_id, post_id=post_id,
                      render_info=render_info, log_message="Avatar image failed the quality gate",
                      layout=layout, brand_kit=brand_kit, signature=signature)


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
                       signature: Optional[str] = None) -> str:
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
    concept's kicker above the headline and ``signature`` as a byline (round 7).
    """
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
                      layout=layout, brand_kit=brand_kit, signature=signature)
