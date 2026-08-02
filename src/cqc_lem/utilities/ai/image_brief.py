"""ONE image-brief author for every surface that renders an AI image.

Replaces the per-surface prompt strings (the lem-simple free paragraph at temperature 1.0, the
carousel keyword-bag) with a single validated builder: the ACTUAL content in, a structured brief
out — a render-ready prompt plus the extracted focal concept the vision gate later grades
against. Written by ``lem-medium``: the brief decides whether the render is relevant at all,
which is exactly the failure the cheapest tier kept producing.

Repo doctrine applies: do NOT add a parallel per-content-type prompt helper — add a preset here.
"""

import json
from dataclasses import dataclass
from typing import Optional

from cqc_lem.utilities.logger import log_debug, log_warning

# Per-surface art direction. The photographic fundamentals (order, lighting, camera, realism
# texture) live in the system prompt; a preset only says what THIS surface is for.
_STYLE_PRESETS: dict[str, str] = {
    "newsletter": (
        "A wide editorial photograph for a LinkedIn newsletter cover. One bold scene drawn from "
        "the edition's core idea, readable at thumbnail size, with environmental depth."),
    "post_image": (
        "A scroll-stopping single-subject photograph for a LinkedIn feed post. One person or "
        "tangible object central to the post's message, one strong color accent."),
    "carousel": (
        "A quiet supporting photograph for ONE carousel slide's single idea. Simple, "
        "uncluttered background a text panel can sit beside — it reinforces, never competes."),
    "video": (
        "An opening frame for a short professional video: a person or subject posed so subtle "
        "motion can bring the frame alive, with layered foreground and background depth."),
    "thumbnail": (
        "A clean, flat product-tutorial thumbnail illustration. Calm palette, simple shapes, "
        "one clear subject."),
}
_DEFAULT_PRESET = "post_image"

# Encodes the BFL/Replicate/fal prompting research (2026): subject-first ordering, 40-80 word
# flowing prose, concrete camera/lighting vocabulary instead of generic quality tags, positive
# phrasing only (FLUX has no negative prompts — naming a thing summons it), and skin texture
# rules that kill the AI sheen. Written to hold for BOTH FLUX.1 LoRA renders and FLUX.2/gpt-image.
_SYSTEM_PROMPT = """Act as a professional photographer writing the brief for ONE real photograph
of LinkedIn visual content. You turn written content into a single render-ready prompt.

### Write the prompt in this order (earlier = more weight)
1. SUBJECT and its action or pose — drawn from the content's actual message, never generic
   office stock. 2. SETTING. 3. COMPOSITION and framing (rule of thirds, eye-level, medium
   shot, negative space — phrased to suit the requested aspect ratio). 4. LIGHTING, precisely
   named — this has the highest impact ("soft window light from camera left with natural
   fill", "golden hour rim light", "large softbox key 45 degrees camera left"). 5. CAMERA:
   one body, one focal length, one aperture ("shot on Sony A7R IV, 85mm f/1.8, ISO 200").
   6. FINISH: film stock or color grade, plus controlled imperfection ("Kodak Portra 400
   tones, subtle film grain, tactile materials").

### Hard rules
- ONE flowing natural-language paragraph of 40-80 words. No keyword stacking, no weight
  syntax, no quotation marks inside the prompt.
- Specificity IS realism. Never lean on generic tags — "photorealistic", "cinematic", "8k",
  "masterpiece", "ultra-detailed" are dead words; concrete gear, named lighting and tactile
  texture do that work.
- Photography vocabulary only. A single word like illustration, painting, render, CGI,
  artstation or stock photo drags the image away from a photograph.
- The renderer ignores negation, so never write "no X" or "without X" — and NEVER mention
  text, letters, numbers, logos, watermarks, brands, charts, or UI at all: naming them
  summons them, garbled. Describe surfaces positively instead: plain unbranded clothing,
  blank screens, clean unmarked walls.
- One cohesive scene — no collages or split screens.
- When a person appears: face clearly visible and well-lit; describe wardrobe, expression
  and pose, never facial features beyond what the context already declares (identity is
  supplied separately and must not be contradicted). Give them natural skin texture with
  visible pores and realistic uneven skin tone — never flawless, porcelain, or smooth skin.
- HANDS are the renderer's weakest anatomy — keep them low-risk or out of frame. Good: arms
  relaxed at the sides, hands resting flat on a desk, framed above the waist, one hand
  loosely holding a simple large object (a mug, a notebook). Never: pointing at the camera,
  open-palm gestures mid-air, interlocked or spread fingers, two hands interacting, or
  hands as the focal point.

### Output
Respond with ONLY a JSON object:
{"focal_concept": "<the one idea the image depicts, under 20 words>",
 "prompt": "<the photograph brief, one 40-80 word paragraph>"}"""

# A refusal or meta-answer leaking into a render prompt produces surreal garbage. Anchored to
# refusal PHRASING on purpose: a bare "language model" entry here rejected every legitimate brief
# an AI-focused author writes ("a dashboard showing large language model routing costs"), so their
# briefs fell back to the deterministic template every time — silently, since the fallback is a
# working code path. Match how a refusal STARTS, never a topic word.
_BANNED_FRAGMENTS = ("i'm sorry", "i am sorry", "i cannot", "i can't", "i am unable",
                     "as an ai", "cannot fulfill", "cannot generate", "unable to generate")

# Added only when the author's own likeness is NOT being rendered. Without it the brief asks for
# "a confident business professional", the renderer supplies an anonymous model, and the result is
# indistinguishable from the stock photography this engine exists to replace — on a PERSONAL-brand
# newsletter a stranger's face is worse than no face. With an avatar, a person IS the point.
_NO_ANONYMOUS_PERSON = (
    "The author's likeness is NOT available for this image, so do NOT make an anonymous person the "
    "focal subject — a generic model standing in an office is exactly the stock-photo look to "
    "avoid. Build the image around a tangible object, a specific environment, or a close-up of "
    "hands mid-action instead. People may appear incidentally, out of focus or from behind, but "
    "never as an identifiable face carrying the frame.\n")

_MIN_PROMPT_CHARS = 60
_MAX_PROMPT_CHARS = 2400

# Generous on purpose. lem-medium is served by a REASONING model (deepseek-v4-flash), whose
# thinking tokens are billed against the same budget before a single character of JSON is
# emitted. At 600 the whole budget went to reasoning and the response came back EMPTY with
# finish_reason='length' — so the brief failed validation, retried, failed again, and every
# image silently rendered from the bland deterministic template. A real brief costs ~870
# completion tokens including reasoning; this leaves headroom for a longer one.
_BRIEF_MAX_TOKENS = 2500


@dataclass
class ImageBrief:
    prompt: str
    ratio: str
    surface: str
    style_preset: str
    focal_concept: str


def _valid(parsed: dict) -> bool:
    prompt = str(parsed.get("prompt") or "").strip()
    focal = str(parsed.get("focal_concept") or "").strip()
    if not (_MIN_PROMPT_CHARS <= len(prompt) <= _MAX_PROMPT_CHARS) or not (3 <= len(focal) <= 300):
        return False
    lowered = prompt.lower()
    return not any(fragment in lowered for fragment in _BANNED_FRAGMENTS)


def _fallback_brief(content: str, *, surface: str, ratio: str, context: str) -> ImageBrief:
    """Deterministic last resort when the brief author is down — bland beats broken."""
    summary = " ".join((content or "").split())[:300]
    direction = _STYLE_PRESETS.get(surface, _STYLE_PRESETS[_DEFAULT_PRESET])
    # Positive phrasing throughout — FLUX ignores negation, so "no logos" summons logos.
    prompt = (f"{context}{direction} A single professional photograph representing: "
              f"{summary}. One clear focal subject, eye-level medium shot composed for a "
              f"{ratio} aspect ratio, shallow depth of field, soft window light with natural "
              f"fill, shot on an 85mm lens at f/1.8, subtle film grain, plain unbranded "
              f"clothing and surfaces, blank screens, clean unmarked walls.")
    return ImageBrief(prompt=prompt, ratio=ratio, surface=surface,
                      style_preset=surface if surface in _STYLE_PRESETS else _DEFAULT_PRESET,
                      focal_concept=summary[:120] or "professional LinkedIn visual")


def build_image_brief(content: str, *, surface: str, ratio: str = "1:1",
                      profile=None, avatar: Optional[dict] = None,
                      extra_direction: Optional[str] = None) -> ImageBrief:
    """Author the brief for one render. Never raises — degrades to a deterministic brief."""
    from cqc_lem.utilities.ai.ai_helper import _call_llm, _profile_visual_context
    from cqc_lem.utilities.avatar.attributes import subject_directive

    preset = surface if surface in _STYLE_PRESETS else _DEFAULT_PRESET
    # The likeness directive leads the context on purpose (issue #744): with nothing stating who
    # a depicted person is, the model invents one and the LoRA renders the invention.
    context = _profile_visual_context(profile, subject_directive(avatar))

    user_prompt = (
        f"{context}{_STYLE_PRESETS[preset]}\n\n"
        f"Compose for a {ratio} aspect ratio.\n"
        + ("" if avatar else _NO_ANONYMOUS_PERSON)
        + (f"Additional direction: {extra_direction}\n" if extra_direction else "")
        + f"\nHere is the content the image must represent:\n<content>{content}</content>")

    reason = "unknown"
    for attempt in (1, 2):
        try:
            response = _call_llm(
                model="lem-medium",
                messages=[{"role": "system", "content": _SYSTEM_PROMPT},
                          {"role": "user", "content": user_prompt}],
                temperature=0.6,
                max_tokens=_BRIEF_MAX_TOKENS,
                response_format={"type": "json_object"},
            )
            choice = response.choices[0]
            raw = choice.message.content or ""
            if not raw.strip():
                # The shape an exhausted token budget takes: finish_reason='length', no content.
                reason = f"empty response (finish_reason={getattr(choice, 'finish_reason', None)})"
                log_debug("Image brief came back empty", surface=surface, attempt=attempt,
                          ai_model="lem-medium", reason=reason)
                continue
            parsed = json.loads(raw)
            if _valid(parsed):
                return ImageBrief(prompt=str(parsed["prompt"]).strip(), ratio=ratio,
                                  surface=surface, style_preset=preset,
                                  focal_concept=str(parsed["focal_concept"]).strip())
            reason = "failed validation (length bounds or refusal phrasing)"
            log_debug("Image brief failed validation — retrying", surface=surface,
                      attempt=attempt, ai_model="lem-medium")
        except Exception as e:
            # One condition, ONE warning: the fallback below carries it — per-attempt noise
            # would double-file the same fault with the escalation cron.
            reason = f"{type(e).__name__}: {e}"[:120]
            log_debug("Image brief attempt failed", error=str(e), ai_model="lem-medium",
                      attempt=attempt)
    # Carry WHY on the warning: this fell back on every generation for weeks and the message
    # alone gave no way to tell an outage from an empty response from a validation reject.
    log_warning("Image brief fell back to the deterministic template", surface=surface,
                action_type="image_brief", reason=reason)
    return _fallback_brief(content, surface=surface, ratio=ratio, context=context)
