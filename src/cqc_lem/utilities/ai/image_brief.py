"""ONE image-brief author for every surface that renders an AI image.

Replaces the per-surface prompt strings (the lem-simple free paragraph at temperature 1.0, the
carousel keyword-bag) with a single validated builder: the ACTUAL content in, a structured brief
out — a render-ready prompt plus the extracted focal concept the vision gate later grades
against. Written by ``lem-medium``: the brief decides whether the render is relevant at all,
which is exactly the failure the cheapest tier kept producing.

Repo doctrine applies: do NOT add a parallel per-content-type prompt helper — add a preset here.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from cqc_lem.utilities.logger import log_debug, log_warning

# Per-surface art direction. The photographic fundamentals (order, lighting, camera, realism
# texture) live in the system prompt; a preset only says what THIS surface is for.
_STYLE_PRESETS: dict[str, str] = {
    "newsletter": (
        "A wide editorial photograph for a LinkedIn newsletter cover. One tangible object or "
        "physical mechanism stands in for the edition's core idea — object-first, macro or "
        "product-photography framing, readable at thumbnail size, with environmental depth. "
        "People and screens stay out of the frame unless the author's own likeness belongs in "
        "this image. For an abstract, financial or software idea, reach for its physical "
        "metaphor: cost or waste as a drip, a leak, a meter or a scale; routing as a switch, a "
        "valve or a fork in a track; a silent failure as a cracked gear or a fallen domino; an "
        "audit as a magnifier, a caliper or a tally counter."),
    "post_image": (
        "A scroll-stopping single-subject photograph for a LinkedIn feed post. One person or "
        "tangible object central to the post's message, one strong color accent."),
    "carousel": (
        "A quiet supporting photograph for ONE carousel slide's single idea. Simple, "
        "uncluttered background a text panel can sit beside — it reinforces, never competes."),
    "video": (
        "An opening frame for a short professional video: a person or subject posed so subtle "
        "motion can bring the frame alive, with layered foreground and background depth."),
    # Photography vocabulary, like every other preset: the shared system prompt below bans
    # "illustration" outright, so a preset asking for one put the two halves of this engine in
    # direct contradiction (issue #1141). A tutorial thumbnail wants CALM, not a different medium.
    "thumbnail": (
        "A bold, high-contrast photograph for a product-tutorial video thumbnail, framed to read "
        "at player-tile size. One tangible object tied to the tutorial's subject, shot close and "
        "off-center on a real, textured working surface rather than an empty backdrop."),
}
_DEFAULT_PRESET = "post_image"

# The ONE vocabulary ban list, named by the system prompt below AND read by the checking side
# (`tests/unit/utilities/ai/test_image_preset_drift.py`) — the writer side and the checking side
# cannot drift, which is exactly how the `thumbnail` preset came to ask for the medium the system
# prompt forbids (issue #1141).
DEAD_QUALITY_TAGS = ("photorealistic", "cinematic", "8k", "masterpiece", "ultra-detailed")
DEAD_STYLE_WORDS = ("illustration", "painting", "render", "CGI", "artstation", "stock photo")

# Negation is not a style question: FLUX has no negative prompting and renders what a prompt
# NAMES, so "no logos" is how a logo gets there. ONE list, for the same reason as the two above —
# both the author-side drift test and the render-side mark guard grep it, so a constraint written
# as a prohibition cannot pass one checker by being invisible to the other.
NEGATION_MARKERS = ("no text", "no logo", "no logos", "without ", "avoid ", "free of ",
                    "don't ", "do not ")

# Encodes the BFL/Replicate/fal prompting research (2026): subject-first ordering, 40-80 word
# flowing prose, concrete camera/lighting vocabulary instead of generic quality tags, positive
# phrasing only (FLUX has no negative prompts — naming a thing summons it), and skin texture
# rules that kill the AI sheen. Written to hold for BOTH FLUX.1 LoRA renders and FLUX.2/gpt-image.
_SYSTEM_PROMPT_HEAD = """Act as a professional photographer writing the brief for ONE real photograph
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
"""

_VOCABULARY_RULES = (
    "- Specificity IS realism. Never lean on generic tags — "
    + ", ".join(DEAD_QUALITY_TAGS)
    + " are dead words; concrete gear, named lighting and tactile texture do that work.\n"
    "- Photography vocabulary only. A single word like "
    + ", ".join(DEAD_STYLE_WORDS)
    + " drags the image away from a photograph.\n")

_SYSTEM_PROMPT_TAIL = """- The renderer ignores negation, so never write "no X" or "without X" — and NEVER mention
  text, letters, numbers, logos, watermarks, brands, charts, or UI at all: naming them
  summons them, garbled. Describe surfaces positively instead: plain unbranded clothing,
  blank screens, clean unmarked walls.
- A SCREEN is where marks appear even when the brief never asked for them: a described laptop,
  monitor or phone invites the renderer to fill its glass with plausible UI, tiled app icons
  and company marks. Build the scene around a tangible object, material or environment rather
  than a screen; when one genuinely belongs in the frame, state that it is switched off and
  uniformly dark.
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

_SYSTEM_PROMPT = _SYSTEM_PROMPT_HEAD + _VOCABULARY_RULES + _SYSTEM_PROMPT_TAIL

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
    "avoid. Build the image around a tangible object or a specific environment instead — never a "
    "close-up of hands, the renderer's weakest anatomy. People may appear incidentally, out of "
    "focus or from behind, but never as an identifiable face carrying the frame.\n")

# Issue #1992: five straight newsletter covers converged on "person at laptop with notebook and
# coffee mug on a wooden desk" despite the preset's "no generic office stock" instruction — a
# negation the brief LLM apparently honors as loosely as the renderer honors negation in the
# final prompt. A newsletter brief that still names one of these (with no avatar in the frame,
# where a person and a desk genuinely may belong) is rejected and retried exactly like a refusal,
# rather than shipped as the same scene the last five editions got.
_STOCK_OFFICE_NOUNS = ("laptop", "notebook", "coffee", "desk", "office", "typing", "keyboard",
                       "screen", "monitor", "phone")
# Word-boundary, not substring: a bare `"phone" in lowered` also rejects "saxophone",
# "microphone", "telephone" and "screening"/"green screen" rejects legitimate metaphors that
# merely contain the noun as a substring rather than naming the cliché object itself.
_STOCK_OFFICE_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(noun) for noun in _STOCK_OFFICE_NOUNS) + r")\b",
    re.IGNORECASE)

# The scene `_fallback_brief` renders for a newsletter cover with no avatar. Purely POSITIVE: the
# generic template's negations ("People and screens stay out of the frame", "blank screens") are
# read by the renderer as things to draw, which is what put a man at a laptop on four of the five
# live sample covers for issue #1992.
_NEWSLETTER_FALLBACK_SCENE = (
    "A wide editorial still-life photograph of one tangible object alone on a worn workshop "
    "surface — a brass valve, a mechanical gauge, a balance scale, a single gear or a hand tool — "
    "filling the frame as the entire subject, an empty workshop wall behind it.")

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
    """One authored image prompt plus the facts the renderer and the vision gate need alongside it.

    `focal_concept` is carried separately because it is what `render_image_gated` scores the render
    AGAINST — a caller that drops it degrades the gate to judging the image against the first 200
    characters of the prompt, which is a far weaker question.

    `prompt` is the brief as written: the no-marks constraint belongs to the renderer
    (`image_gen.with_no_marks`), which phrases it per backend, so it is never baked in here.
    `style_preset` is the preset that was actually applied, which is `surface` only when that
    surface has one — otherwise it is the default, and the two differ.
    `fallback` is True only when the deterministic template shipped because the author LLM never
    produced a usable brief — the field a stored brief receipt needs to be auditable (issue #1992).
    """

    prompt: str
    ratio: str
    surface: str
    style_preset: str
    focal_concept: str
    fallback: bool = False


def _valid(parsed: dict[str, Any], *, surface: Optional[str] = None,
          avatar: Optional[dict[str, Any]] = None) -> bool:
    prompt = str(parsed.get("prompt") or "").strip()
    focal = str(parsed.get("focal_concept") or "").strip()
    if not (_MIN_PROMPT_CHARS <= len(prompt) <= _MAX_PROMPT_CHARS) or not (3 <= len(focal) <= 300):
        return False
    lowered = prompt.lower()
    if any(fragment in lowered for fragment in _BANNED_FRAGMENTS):
        return False
    # Newsletter only, and only with no avatar in frame — a person at a desk is a legitimate
    # scene once the author's own likeness is the point (issue #1992).
    if surface == "newsletter" and not avatar:
        hit = _STOCK_OFFICE_PATTERN.search(lowered)
        if hit:
            log_debug("Newsletter brief rejected — stock-office noun", noun=hit.group(0))
            return False
    return True


def _fallback_brief(content: str, *, surface: str, ratio: str, context: str,
                    avatar: Optional[dict[str, Any]] = None) -> ImageBrief:
    """Deterministic last resort when the brief author is down — bland beats broken.

    The newsletter surface with no avatar gets its OWN scene, because this template is a RENDER
    prompt and every noun in it is a request: the generic one pasted in the surface's instruction
    preset ("People and screens stay out of the frame"), then asked for "blank screens" and "plain
    unbranded clothing", and the renderer duly produced a man at a laptop — the exact scene issue
    #1992 was filed about, on 4 of 5 live editions.
    """
    summary = " ".join((content or "").split())[:300]
    if surface == "newsletter" and not avatar:
        # Strip the edition's own stock-office nouns out of the summary too: the hook supplies
        # them, and the renderer draws whatever the summary names.
        summary = " ".join(_STOCK_OFFICE_PATTERN.sub(" ", summary).split())
        prompt = (f"{_NEWSLETTER_FALLBACK_SCENE} The object stands in for this idea: {summary}. "
                  f"Macro still-life composed for a {ratio} aspect ratio, dramatic raking side "
                  f"light, shallow depth of field, shot on a 100mm macro lens at f/2.8, tactile "
                  f"aged metal and worn wood texture, subtle film grain, clean unmarked "
                  f"surfaces.")
        return ImageBrief(prompt=prompt, ratio=ratio, surface=surface, style_preset=surface,
                          focal_concept=summary[:120] or "a single tangible object",
                          fallback=True)
    direction = _STYLE_PRESETS.get(surface, _STYLE_PRESETS[_DEFAULT_PRESET])
    # Positive phrasing throughout — FLUX ignores negation, so "no logos" summons logos.
    prompt = (f"{context}{direction} A single professional photograph representing: "
              f"{summary}. One clear focal subject, eye-level medium shot composed for a "
              f"{ratio} aspect ratio, shallow depth of field, soft window light with natural "
              f"fill, shot on an 85mm lens at f/1.8, subtle film grain, plain unbranded "
              f"clothing and surfaces, blank screens, clean unmarked walls.")
    return ImageBrief(prompt=prompt, ratio=ratio, surface=surface,
                      style_preset=surface if surface in _STYLE_PRESETS else _DEFAULT_PRESET,
                      focal_concept=summary[:120] or "professional LinkedIn visual",
                      fallback=True)


def build_image_brief(content: str, *, surface: str, ratio: str = "1:1",
                      profile=None, avatar: Optional[dict[str, Any]] = None,
                      extra_direction: Optional[str] = None,
                      content_shape: Optional[str] = None,
                      avoid_terms: Optional[list[str]] = None) -> ImageBrief:
    """Author the brief for one render. Never raises — degrades to a deterministic brief.

    ``content_shape`` (issue #1992) is a short caller-supplied tag — a newsletter edition's
    format + hook style — folded into the context so the brief distinguishes a listicle cover
    from a personal-story one; unused by callers that have no equivalent shape.

    ``avoid_terms`` are nouns the caller already knows are wrong for this image — the concepts of
    the last few covers, the generic nouns the edition's own hook keeps repeating. They reach the
    AUTHOR only and never the deterministic fallback, because a fallback prompt goes straight to a
    renderer, which reads every noun in it as a request.
    """
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
        + (f"Content shape: {content_shape}\n" if content_shape else "")
        + (f"Do NOT build the scene around any of these, and do not name them in the prompt: "
           f"{'; '.join(avoid_terms)}\n" if avoid_terms else "")
        + (f"Additional direction: {extra_direction}\n" if extra_direction else "")
        + f"\nHere is the content the image must represent:\n<content>{content}</content>")

    reason = "unknown"
    for attempt in (1, 2):
        try:
            # The retry carries WHY the last attempt was thrown out. Re-sending the identical
            # prompt just re-drew the same rejected scene: on the five live editions of issue
            # #1992 the second attempt named a stock-office noun as often as the first, and four
            # of five covers shipped from the deterministic fallback because of it.
            retry_note = ("" if attempt == 1 else
                          f"\n\nYour previous attempt was REJECTED: {reason}. Write a different "
                          f"scene built on a different object — do not repeat the rejected one.")
            response = _call_llm(
                model="lem-medium",
                messages=[{"role": "system", "content": _SYSTEM_PROMPT},
                          {"role": "user", "content": user_prompt + retry_note}],
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
            if _valid(parsed, surface=surface, avatar=avatar):
                return ImageBrief(prompt=str(parsed["prompt"]).strip(), ratio=ratio,
                                  surface=surface, style_preset=preset,
                                  focal_concept=str(parsed["focal_concept"]).strip(),
                                  fallback=False)
            # Name the offending noun when that is what failed: it is what makes the retry note
            # above actionable rather than a repeat of the same instruction.
            hit = (_STOCK_OFFICE_PATTERN.search(str(parsed.get("prompt") or ""))
                   if surface == "newsletter" and not avatar else None)
            reason = (f"the prompt named the stock-office object {hit.group(0)!r}" if hit else
                      "failed validation (length bounds or refusal phrasing)")
            log_debug("Image brief failed validation — retrying", surface=surface,
                      attempt=attempt, ai_model="lem-medium", reason=reason)
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
    return _fallback_brief(content, surface=surface, ratio=ratio, context=context, avatar=avatar)
