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

# Per-surface art direction. Composition/lighting fundamentals live in the system prompt; a
# preset only says what THIS surface is for.
_STYLE_PRESETS: dict[str, str] = {
    "newsletter": (
        "A cinematic, editorial magazine-cover-worthy WIDE scene for a LinkedIn newsletter "
        "edition. It must telegraph the edition's core idea at thumbnail size — bold, "
        "atmospheric, instantly readable."),
    "post_image": (
        "A scroll-stopping single-subject image for a LinkedIn feed post. One person or "
        "tangible object central to the post's message, strong color accent, credible and "
        "professional."),
    "carousel": (
        "A clean supporting visual for ONE carousel slide's single idea. Simple, uncluttered "
        "background that a text panel can sit beside — the image reinforces, never competes."),
    "video": (
        "A photorealistic opening frame for a short professional video. A person or subject "
        "positioned to come alive with subtle motion, cinematic depth."),
    "thumbnail": (
        "A clean, flat product-tutorial thumbnail illustration. Calm palette, simple shapes, "
        "one clear subject."),
}
_DEFAULT_PRESET = "post_image"

_SYSTEM_PROMPT = """Act as a world-class commercial visual director creating scroll-stopping
LinkedIn imagery. You turn written content into a brief for a professional photoshoot.

### Required qualities
- ONE clear focal subject in the foreground, drawn from the content's actual message — never
  generic office stock. When a person is present, they make confident eye contact with the
  camera.
- Attention-drawing composition: strong foreground/background separation, shallow depth of
  field, a bold high-contrast color accent that pops in a busy feed.
- Professional & on-brand: modern, clean, credible for the author's stated industry.
  Photorealistic by default; tasteful editorial illustration only when it clearly fits.
- Specific and grounded — not abstract, surreal, or symbolic clip-art.

### Hard constraints
- NO text, letters, words, numbers, logos, watermarks, captions, charts, or UI anywhere in the
  image — generators render these as garbled artifacts.
- No collages, split screens, or busy montages — one cohesive scene.

### Output
Respond with ONLY a JSON object:
{"focal_concept": "<the one idea the image depicts, under 20 words>",
 "prompt": "<one richly descriptive render-ready paragraph: scene, subject, setting, lighting, color>"}"""

# A refusal or meta-answer leaking into a render prompt produces surreal garbage. Anchored to
# refusal PHRASING on purpose: a bare "language model" entry here rejected every legitimate brief
# an AI-focused author writes ("a dashboard showing large language model routing costs"), so their
# briefs fell back to the deterministic template every time — silently, since the fallback is a
# working code path. Match how a refusal STARTS, never a topic word.
_BANNED_FRAGMENTS = ("i'm sorry", "i am sorry", "i cannot", "i can't", "i am unable",
                     "as an ai", "cannot fulfill", "cannot generate", "unable to generate")

_MIN_PROMPT_CHARS = 60
_MAX_PROMPT_CHARS = 2400


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
    prompt = (f"{context}{direction} A single photorealistic, professional scene representing: "
              f"{summary}. One clear focal subject, shallow depth of field, natural lighting, "
              f"bold color accent, composed for a {ratio} aspect ratio. No text, letters, "
              f"logos, watermarks, charts, or UI anywhere in the image.")
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
        + (f"Additional direction: {extra_direction}\n" if extra_direction else "")
        + f"\nHere is the content the image must represent:\n<content>{content}</content>")

    for attempt in (1, 2):
        try:
            response = _call_llm(
                model="lem-medium",
                messages=[{"role": "system", "content": _SYSTEM_PROMPT},
                          {"role": "user", "content": user_prompt}],
                temperature=0.6,
                max_tokens=600,
                response_format={"type": "json_object"},
            )
            parsed = json.loads(response.choices[0].message.content)
            if _valid(parsed):
                return ImageBrief(prompt=str(parsed["prompt"]).strip(), ratio=ratio,
                                  surface=surface, style_preset=preset,
                                  focal_concept=str(parsed["focal_concept"]).strip())
            log_debug("Image brief failed validation — retrying", surface=surface,
                      attempt=attempt, ai_model="lem-medium")
        except Exception as e:
            # One condition, ONE warning: the fallback below carries it — per-attempt noise
            # would double-file the same fault with the escalation cron.
            log_debug("Image brief attempt failed", error=str(e), ai_model="lem-medium",
                      attempt=attempt)
    log_warning("Image brief fell back to the deterministic template", surface=surface,
                action_type="image_brief")
    return _fallback_brief(content, surface=surface, ratio=ratio, context=context)
