"""ONE image-brief author for every surface that renders an AI image.

Stage 2 of the staged image engine (``docs/image-stack.md``). Stage 1
(``image_concept.analyze_content_for_image``) reads the whole piece and returns its thesis, the
concrete entities it names, a hook, and a TREATMENT. This module turns that into one render-ready
prompt, validated deterministically and — when a concept is in hand — checked once more by a cheap
judge (Stage 3, ``check_prompt_against_concept``) before a single image is paid for.

The brief no longer asks for a "physical metaphor": that request is what put a valve, a gauge or
a pipe on every newsletter cover whatever the edition said (issue #2241). Stock symbols are now
REFUSED outright, through the ONE ``CLICHE_OBJECTS`` list both this author and the vision gate
read.

Repo doctrine applies: do NOT add a parallel per-content-type prompt helper — add a preset here.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from cqc_lem.utilities.ai.image_concept import (
    TREATMENT_CONCRETE,
    TREATMENT_GRAPHIC,
    TREATMENT_METAPHOR,
    TREATMENT_PEOPLE,
    WEAK_ENTITY_FLOOR,
    ImageConcept,
    analyze_content_for_image,
    entity_mentioned,
)
from cqc_lem.utilities.logger import log_debug, log_warning
from cqc_lem.utilities.observability import llm_step

# Per-surface USE CASE: what the image is for and where it has to read. The visual approach is
# the TREATMENT's job (below) — a surface preset never offers subjects, because a menu of objects
# is exactly what every cover then picked from (issue #2241).
_STYLE_PRESETS: dict[str, str] = {
    "newsletter": (
        "A LinkedIn newsletter cover at 16:9. The subject must still read at a 400x225 "
        "thumbnail, so keep it large and keep everything that matters inside the central 60% "
        "of the frame."),
    "post_image": (
        "A LinkedIn feed post image that has to stop a scroll at phone width: one strong "
        "subject, one strong color accent, legible at a glance."),
    "carousel": (
        "A quiet supporting image for ONE carousel slide's single idea, on a simple, "
        "uncluttered background a text panel can sit beside — it reinforces the slide and "
        "never competes with it."),
    "video": (
        "The opening frame of a short professional video: a subject posed so subtle motion can "
        "bring the frame alive, with layered foreground and background depth."),
    # Photography vocabulary, like every other preset: the shared system prompt bans the
    # illustration medium outright (issue #1141). A tutorial thumbnail wants CALM, not a
    # different medium.
    "thumbnail": (
        "A bold, high-contrast product-tutorial video thumbnail framed to read at player-tile "
        "size, its subject large and off-center on a real, textured working surface."),
}
_DEFAULT_PRESET = "post_image"

# Short noun for each surface, used by the deterministic fallback's opening clause.
_USE_LABELS: dict[str, str] = {
    "newsletter": "LinkedIn newsletter cover",
    "post_image": "LinkedIn feed post",
    "carousel": "LinkedIn carousel slide",
    "video": "short professional video",
    "thumbnail": "tutorial video thumbnail",
}

# The visual approach, chosen by Stage 1 from what the piece actually is.
_TREATMENT_TEMPLATES: dict[str, str] = {
    TREATMENT_PEOPLE: (
        "TREATMENT people_scene: a real, candid documentary photograph of the piece's audience "
        "in the exact situation it describes, built from its specific entities — people caught "
        "mid-task with natural expressions, available light and imperfect real-world texture. "
        "When the author's likeness is supplied, the author is the person in the scene."),
    TREATMENT_GRAPHIC: (
        "TREATMENT editorial_graphic: a designed editorial graphic. The ONLY words in the image "
        "are the hook, in double quotes, spelled exactly, set in bold sans-serif type with its "
        "placement stated (for example large in the left third). One supporting photographic "
        "element drawn from the specific entities sits beside it on a flat brand-palette color "
        "field with generous negative space."),
    TREATMENT_CONCRETE: (
        "TREATMENT concrete_scene: a photograph of the specific, tangible situation the "
        "entities describe, shot where it really happens — the actual objects, documents and "
        "setting the piece names. A screen may appear when it IS the subject."),
    TREATMENT_METAPHOR: (
        "TREATMENT metaphor_last_resort: one uncommon visual metaphor specific to this piece's "
        "thesis, photographed as a real scene — one a reader has never seen on a hundred other "
        "covers. Stock symbols are refused."),
}

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

# The ONE cliché list (issue #2241). Every stock symbol a LinkedIn image reaches for when it has
# nothing specific to say — and the stock-office scene issue #1992 was filed about. Read by the
# author's system prompt, by `_rejection` (whole prompt, every surface, the avatar path too), by
# the deterministic fallback (scrubbed out of anything it interpolates), and by the vision gate's
# targeted question. A multi-word entry matches with an optional article between its words, and
# its LAST word in any inflection ("pipes", "piping", "dripping").
CLICHE_OBJECTS: tuple[str, ...] = (
    "pipe", "plumbing", "valve", "faucet", "drip", "leak", "gear", "cog", "gauge",
    "lightbulb", "light bulb", "puzzle piece", "jigsaw", "handshake", "chess", "chessboard",
    "chess piece", "robot", "android", "glowing brain", "neural network glow",
    "glowing neural network", "circuit board", "binary code", "matrix code", "rocket", "target",
    "dartboard", "bullseye", "mountain summit", "mountain peak", "compass", "hourglass", "domino",
    "chain link", "maze", "ladder", "key and lock", "lock and key", "padlock", "crystal ball",
    "magnifying glass",
    # Stock office (issue #1992).
    "person at laptop", "man at laptop", "woman at laptop", "professional at laptop",
    "typing hands", "hands typing", "hands on keyboard", "coffee and notebook",
)
# "target audience" is a reader, not a dartboard — the business senses never count as the symbol.
_TARGET_BUSINESS_SENSE = re.compile(
    r"\s+(?:audience|market|customer|client|account|reader|buyer|segment|persona|date|number|"
    r"metric|list|role)s?\b", re.IGNORECASE)
_PHRASE_GAP = r"\s+(?:(?:a|an|the|their|his|her|its|on|at|and)\s+)?"


def _inflections(term: str) -> set[str]:
    """`pipe` must also catch `pipes` and `piping`, `drip` must catch `dripping` (issue #2241)."""
    forms = {term, f"{term}s", f"{term}es", f"{term}ing", f"{term}y"}
    if term.endswith("e"):
        forms.add(f"{term[:-1]}ing")
    if len(term) >= 3 and term[-1] not in "aeiouwy" and term[-2] in "aeiou":
        forms.update({f"{term}{term[-1]}ing", f"{term}{term[-1]}y"})
    return forms


def _phrase_pattern(phrase: str) -> str:
    *head, last = phrase.lower().split()
    tail = "(?:" + "|".join(re.escape(f) for f in sorted(_inflections(last), key=len,
                                                          reverse=True)) + ")"
    return _PHRASE_GAP.join([re.escape(w) for w in head] + [tail])


_CLICHE_PATTERN = re.compile(
    r"\b(?:" + "|".join(_phrase_pattern(p) for p in sorted(CLICHE_OBJECTS, key=len, reverse=True))
    + r")\b", re.IGNORECASE)
_COFFEE = re.compile(r"\bcoffee\b", re.IGNORECASE)
_NOTEBOOK = re.compile(r"\bnotebooks?\b", re.IGNORECASE)


def cliche_hit(text: Optional[str]) -> Optional[str]:
    """The first stock symbol ``text`` names, or None.

    Args:
        text: A render prompt, an entity, or a vision judge's description.

    Returns:
        The matched words as written (e.g. ``"dripping"``), or ``"coffee and notebook"`` when both
        halves of that stock pairing appear anywhere in the text.
    """
    text = text or ""
    for match in _CLICHE_PATTERN.finditer(text):
        word = match.group(0)
        if word.lower().startswith("target") and _TARGET_BUSINESS_SENSE.match(text, match.end()):
            continue
        return word
    if _COFFEE.search(text) and _NOTEBOOK.search(text):
        return "coffee and notebook"
    return None


def _scrub_cliches(text: str) -> str:
    """``text`` with every stock symbol removed — for anything the fallback interpolates."""
    scrubbed = _CLICHE_PATTERN.sub(" ", text or "")
    return " ".join(scrubbed.replace('"', "").replace("“", "").replace("”", "").split())


# Encodes the OpenAI gpt-image and BFL FLUX prompting guides (2026): state the use case, order the
# prompt scene -> subject -> details -> constraints, quote any text exactly with its placement and
# type, and get realism from camera language and real texture rather than quality tags. Positive
# phrasing only (FLUX has no negative prompts — naming a thing summons it).
_SYSTEM_PROMPT_HEAD = """Act as a photo editor and art director writing the brief for ONE image \
of LinkedIn visual content. You receive the use case, a TREATMENT, an analysis of the piece \
(thesis, audience, specific entities, emotional beat, hook) and an excerpt of the piece itself, \
and you turn them into a single render-ready prompt.

### Write the prompt in this order (earlier = more weight)
1. SCENE: what this image is for and where it takes place, as the use case asks.
2. SUBJECT: who or what carries the frame — built from the piece's SPECIFIC ENTITIES, never a
   generic stand-in or a symbol. 3. DETAILS: composition for the requested aspect ratio;
   LIGHTING precisely named ("soft window light from camera left with natural fill"); CAMERA,
   one body, one focal length, one aperture ("shot on Sony A7R IV, 35mm f/2"); FINISH with real
   texture and controlled imperfection ("Kodak Portra 400 tones, subtle film grain"). For an
   editorial graphic: the type weight, the color fields and exactly where the hook sits.
4. CONSTRAINTS last.

### Hard rules
- ONE flowing natural-language paragraph of 40-90 words. No keyword stacking, no weight syntax.
- Depict at least two of the specific entities literally, and name the ones you used in
  required_entities.
- Stock symbols are REFUSED, whatever the topic: """ + ", ".join(CLICHE_OBJECTS) + """. Find
  what THIS piece names instead.
- Quotation marks appear ONLY around the hook on the editorial_graphic treatment, copied exactly.
  Every other treatment carries no quoted text and no hook.
"""

_VOCABULARY_RULES = (
    "- Specificity IS realism. Never lean on generic tags — "
    + ", ".join(DEAD_QUALITY_TAGS)
    + " are dead words; concrete gear, named lighting and tactile texture do that work.\n"
    "- Photography and editorial-design vocabulary only. A single word like "
    + ", ".join(DEAD_STYLE_WORDS)
    + " drags the image away from a real photograph or a designed graphic.\n")

_SYSTEM_PROMPT_TAIL = """- The renderer ignores negation, so never write "no X" or "without X" — and NEVER mention
  text, letters, numbers, logos, watermarks, brands, charts, or UI at all, the editorial_graphic
  hook being the one exception: naming them summons them, garbled. Describe surfaces positively
  instead: plain unbranded clothing, clean unmarked walls.
- A SCREEN is where marks appear even when the brief never asked for them: a described laptop,
  monitor or phone invites the renderer to fill its glass with plausible UI, tiled app icons and
  company marks. Use one only when it genuinely IS the subject, and then put the weight on the
  person's reaction and the room around it rather than on what the glass shows.
- One cohesive scene — no collages or split screens.
- When a person appears: face clearly visible and well-lit; describe wardrobe, expression
  and pose, never facial features beyond what the context already declares (identity is
  supplied separately and must not be contradicted). Give them natural skin texture with
  visible pores and realistic uneven skin tone — never flawless, porcelain, or smooth skin.
- HANDS are the renderer's weakest anatomy — keep them low-risk or out of frame. Good: arms
  relaxed at the sides, hands resting flat on a desk, framed above the waist, one hand
  loosely holding a simple large object (a mug, a folder). Never: pointing at the camera,
  open-palm gestures mid-air, interlocked or spread fingers, two hands interacting, or
  hands as the focal point.

### Output
Respond with ONLY a JSON object:
{"focal_concept": "<the one idea the image depicts, under 20 words>",
 "prompt": "<the brief, one 40-90 word paragraph>",
 "required_entities": ["<each specific entity the prompt depicts>"],
 "hook_text": "<the hook exactly as quoted in the prompt, or null>"}"""

_SYSTEM_PROMPT = _SYSTEM_PROMPT_HEAD + _VOCABULARY_RULES + _SYSTEM_PROMPT_TAIL

# A refusal or meta-answer leaking into a render prompt produces surreal garbage. Anchored to
# refusal PHRASING on purpose: a bare "language model" entry here rejected every legitimate brief
# an AI-focused author writes ("a dashboard showing large language model routing costs"), so their
# briefs fell back to the deterministic template every time — silently, since the fallback is a
# working code path. Match how a refusal STARTS, never a topic word.
_BANNED_FRAGMENTS = ("i'm sorry", "i am sorry", "i cannot", "i can't", "i am unable",
                     "as an ai", "cannot fulfill", "cannot generate", "unable to generate")

# Added only when the author's own likeness is NOT being rendered. The old directive here forbade
# any identifiable person outright, and together with the stock-office gate it left the author
# one move — an object still-life — which is how every cover became a valve (issue #2241).
# LinkedIn's own guidance is that real people beat symbols; what reads as stock is the POSED model.
_NO_AVATAR_PEOPLE = (
    "The author's likeness is NOT available for this image. People may appear as real "
    "participants caught mid-task — candid, partly turned, absorbed in the situation — never "
    "as a posed model smiling at the camera, which reads as stock photography.\n")

_MIN_PROMPT_CHARS = 60
_MAX_PROMPT_CHARS = 2400
_EXCERPT_CHARS = 3000
# Attempts at a brief: the first, plus up to two retries that carry the rejection reason.
_BRIEF_ATTEMPTS = 3
# A capped soft-steer list, so a long history never crowds out the piece itself.
_MAX_AVOID_TERMS = 8

# Generous on purpose. lem-medium is served by a REASONING model (deepseek-v4-flash), whose
# thinking tokens are billed against the same budget before a single character of JSON is
# emitted. At 600 the whole budget went to reasoning and the response came back EMPTY with
# finish_reason='length' — so the brief failed validation, retried, failed again, and every
# image silently rendered from the bland deterministic template. A real brief costs ~870
# completion tokens including reasoning; this leaves headroom for a longer one.
_BRIEF_MAX_TOKENS = 2500
# Stage 3's judge is lem-simple, also a reasoning model.
_CHECK_MAX_TOKENS = 1500

_QUOTED = re.compile(r'"([^"\n]{1,200})"|“([^”\n]{1,200})”')

_PALETTE_DEFAULT = "deep navy and warm off-white"


@dataclass
class ImageBrief:
    """One authored image prompt plus the facts the renderer and the vision gate need alongside it.

    `focal_concept` is what the legacy vision gate scores against when no concept is in hand.
    `prompt` is the brief as written: the no-marks constraint belongs to the renderer
    (`image_gen.with_no_marks`), which phrases it per backend, so it is never baked in here.
    `style_preset` is the preset that was actually applied, which is `surface` only when that
    surface has one — otherwise it is the default, and the two differ.
    `fallback` is True only when the deterministic template shipped because the author LLM never
    produced a usable brief — the field a stored brief receipt needs to be auditable (issue #1992).

    `concept` is Stage 1's analysis (None when it was unavailable), `treatment` the approach the
    brief took, `required_entities` the concept entities the prompt actually depicts, `hook_text`
    the ONE string the render may carry (editorial_graphic only), and `prompt_check` what Stage 3
    said — all recorded on the brief receipt (issue #2241).
    """

    prompt: str
    ratio: str
    surface: str
    style_preset: str
    focal_concept: str
    fallback: bool = False
    concept: Optional[ImageConcept] = None
    treatment: Optional[str] = None
    required_entities: tuple[str, ...] = ()
    hook_text: Optional[str] = None
    prompt_check: Optional[str] = None


def usable_entities(concept: Optional[ImageConcept]) -> list[str]:
    """The concept's entities minus any stock symbol.

    A cliché is refused even when the text names it, so asking a prompt to depict one would make
    the brief unpassable. Shared with the vision gate, which asks about the same entities.

    Args:
        concept: Stage 1's analysis, or None.

    Returns:
        The entities a prompt may be asked to depict; empty without a concept.
    """
    if concept is None:
        return []
    return [e for e in concept.specific_entities if not cliche_hit(e)]


def _treatment_for(concept: Optional[ImageConcept], avatar: Optional[dict[str, Any]]) -> str:
    """The treatment this brief takes.

    The author's likeness means a person IS the subject, and the LoRA path cannot set type, so an
    avatar always takes ``people_scene``. With no concept, a concrete scene from the excerpt.
    """
    if avatar:
        return TREATMENT_PEOPLE
    if concept is None:
        return TREATMENT_CONCRETE
    return concept.treatment


def _quoted_strings(prompt: str) -> list[str]:
    return [(a or b).strip() for a, b in _QUOTED.findall(prompt or "")]


def _deterministic_failure(prompt: str, *, entities: list[str], weak: bool,
                           hook_text: Optional[str]) -> Optional[str]:
    """Why ``prompt`` fails the deterministic checks, or None when it passes them.

    Shared by Stage 2's validation and Stage 3, so the two can never disagree about a cliché,
    a stray quote or entity coverage.
    """
    hit = cliche_hit(prompt)
    if hit:
        return (f"the prompt named the stock symbol {hit!r} — build the image on the piece's "
                f"own entities instead")
    quoted = _quoted_strings(prompt)
    if hook_text:
        if not any(q == hook_text for q in quoted):
            return f'the hook must appear in double quotes exactly as "{hook_text}"'
        stray = [q for q in quoted if q != hook_text]
        if stray:
            return f"the prompt quotes text other than the hook: {stray[0]!r}"
    elif quoted:
        return (f"the prompt quotes text ({quoted[0]!r}) but this treatment carries no words "
                f"at all")
    if not weak and len(entities) >= WEAK_ENTITY_FLOOR:
        depicted = [e for e in entities if entity_mentioned(e, prompt)]
        if len(depicted) < WEAK_ENTITY_FLOOR:
            return (f"the prompt depicts {len(depicted)} of the piece's specific entities; show "
                    f"at least two of: {'; '.join(entities)}")
    return None


def _rejection(parsed: dict[str, Any], *, entities: list[str], weak: bool,
               hook_text: Optional[str]) -> Optional[str]:
    """Why an authored brief is unusable, or None. The reason goes back to the author verbatim."""
    prompt = str(parsed.get("prompt") or "").strip()
    focal = str(parsed.get("focal_concept") or "").strip()
    if not (_MIN_PROMPT_CHARS <= len(prompt) <= _MAX_PROMPT_CHARS) or not (3 <= len(focal) <= 300):
        return "failed validation (length bounds)"
    lowered = prompt.lower()
    if any(fragment in lowered for fragment in _BANNED_FRAGMENTS):
        return "failed validation (refusal phrasing)"
    reply_hook = str(parsed.get("hook_text") or "").strip()
    if reply_hook.lower() not in ("", "null", "none") and not hook_text:
        return "the reply declared hook text, but only the editorial_graphic treatment has a hook"
    return _deterministic_failure(prompt, entities=entities, weak=weak, hook_text=hook_text)


_CHECK_PROMPT = """You are checking an image prompt BEFORE it is sent to a renderer.

The piece's thesis: {thesis}
Its specific entities: {entities}

The prompt:
<prompt>{prompt}</prompt>

Would an image rendered from this prompt let a stranger guess the piece's thesis? Name which of
the entities the prompt depicts. Respond with ONLY a JSON object:
{{"guessable": true|false, "depicted_entities": ["..."], "reason": "<one short sentence>"}}"""


@llm_step("image_prompt_check")
def check_prompt_against_concept(prompt: str, concept: Optional[ImageConcept],
                                 hook_text: Optional[str]) -> tuple[bool, str]:
    """Stage 3: is this prompt worth rendering? Deterministic checks first, then ONE judge call.

    The deterministic half (stock symbols, stray quoted text, entity coverage) always applies. The
    ``lem-simple`` judge then asks whether a stranger could guess the thesis from the image; it
    fails OPEN — an unreachable judge passes the prompt, since the vision gate still stands behind.

    Args:
        prompt: The authored render prompt.
        concept: Stage 1's analysis; without one only the deterministic checks run.
        hook_text: The one string the image may carry, or None.

    Returns:
        ``(ok, reason)`` — ``reason`` is the repair directive when ``ok`` is False.
    """
    entities = usable_entities(concept)
    weak = concept is None or concept.weak or len(entities) < WEAK_ENTITY_FLOOR
    failure = _deterministic_failure(prompt, entities=entities, weak=weak, hook_text=hook_text)
    if failure:
        return False, failure
    if concept is None:
        return True, "deterministic checks passed (no concept for the judge)"

    from cqc_lem.utilities.ai.ai_helper import _loads_json_object
    from cqc_lem.utilities.ai.client import client

    try:
        response = client.chat.completions.create(
            model="lem-simple",
            messages=[{"role": "user", "content": _CHECK_PROMPT.format(
                thesis=concept.thesis, entities="; ".join(entities) or "(none named)",
                prompt=prompt)}],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=_CHECK_MAX_TOKENS,
        )
        verdict = _loads_json_object(response.choices[0].message.content or "")
    except Exception as e:
        log_debug("Image prompt judge unavailable — passing the prompt", error=str(e),
                  action_type="image_brief")
        return True, "judge unavailable"
    if not isinstance(verdict, dict) or "guessable" not in verdict:
        return True, "judge reply unusable"
    if verdict.get("guessable") is False:
        why = " ".join(str(verdict.get("reason") or "").split())[:200]
        return False, ("a stranger could not guess the thesis from this prompt"
                       + (f": {why}" if why else "") + f" — show {concept.thesis}")
    return True, "judge passed"


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _fallback_brief(content: str, *, surface: str, ratio: str, context: str,
                    avatar: Optional[dict[str, Any]] = None,
                    concept: Optional[ImageConcept] = None,
                    treatment: Optional[str] = None,
                    brand_kit: Optional[str] = None) -> ImageBrief:
    """Deterministic last resort when the brief author is down — built from the piece, not a set.

    Every noun here is a RENDER request, so it is assembled only from the treatment and the
    concept's own grounded entities, with every stock symbol scrubbed out of anything
    interpolated. The old newsletter fallback was a workshop still-life (a brass valve, a gauge, a
    gear) — the scene issue #2241 was filed about — and it is gone.
    """
    treatment = treatment or _treatment_for(concept, avatar)
    entities = [_scrub_cliches(e) for e in usable_entities(concept)]
    entities = [e for e in entities if e]
    use = _USE_LABELS.get(surface, _USE_LABELS[_DEFAULT_PRESET])
    summary = _scrub_cliches(concept.thesis if concept else content)[:240]
    hook = concept.hook_phrase if concept and treatment == TREATMENT_GRAPHIC else None
    finish = (f"composed for a {ratio} aspect ratio with the subject large in the central frame, "
              f"soft window light with natural fill, subtle film grain, tactile real-world "
              f"texture, plain unbranded surfaces, clean unmarked walls.")

    if treatment == TREATMENT_GRAPHIC and hook:
        support = entities[0] if entities else summary[:80]
        palette = brand_kit or _PALETTE_DEFAULT
        prompt = (f'{context}A designed editorial graphic for a {use}: bold sans-serif type '
                  f'reading "{hook}" set large in the left third, beside one photographic '
                  f'cutout of {support} on a flat color field in {palette}, generous negative '
                  f'space, high contrast, composed for a {ratio} aspect ratio.')
    elif treatment == TREATMENT_PEOPLE:
        who = _scrub_cliches(concept.audience) if concept and concept.audience else ""
        who = who or "a small working team"
        around = _join(entities[:3]) if entities else f"the situation this describes: {summary}"
        prompt = (f"{context}A candid documentary photograph for a {use}: {who} caught "
                  f"mid-conversation around {around}, in a real working setting, eye-level "
                  f"medium shot, shot on a 35mm lens at f/2, natural skin texture, plain "
                  f"unbranded clothing, {finish}")
    elif len(entities) >= 2:
        extra = f", with {entities[2]} in the frame" if len(entities) > 2 else ""
        prompt = (f"{context}A candid editorial photograph for a {use} of {entities[0]} and "
                  f"{entities[1]} in the real place this happens{extra}, shot on a 50mm lens "
                  f"at f/2.8, {finish}")
    else:
        prompt = (f"{context}A candid editorial photograph for a {use} of the real, specific "
                  f"situation this describes: {summary}. One clear focal subject, shot on a 50mm "
                  f"lens at f/2.8, {finish}")
    return ImageBrief(prompt=prompt, ratio=ratio, surface=surface,
                      style_preset=surface if surface in _STYLE_PRESETS else _DEFAULT_PRESET,
                      focal_concept=(summary[:120] or "professional LinkedIn visual"),
                      fallback=True, concept=concept, treatment=treatment,
                      required_entities=tuple(e for e in entities if entity_mentioned(e, prompt)),
                      hook_text=hook)


def _analysis_block(concept: Optional[ImageConcept], entities: list[str],
                    hook: Optional[str]) -> str:
    if concept is None:
        return ("Analysis of the piece: unavailable — read the excerpt and build the image on "
                "the specific things it names.\n")
    analysis = {"thesis": concept.thesis, "audience": concept.audience,
                "specific_entities": entities, "emotional_beat": concept.emotional_beat}
    block = f"Analysis of the piece: {json.dumps(analysis)}\n"
    if hook:
        block += f'Hook — the ONLY text in the image, quoted exactly: "{hook}"\n'
    return block


def build_image_brief(content: str, *, surface: str, ratio: str = "1:1",
                      profile=None, avatar: Optional[dict[str, Any]] = None,
                      extra_direction: Optional[str] = None,
                      content_shape: Optional[str] = None,
                      avoid_terms: Optional[list[str]] = None,
                      concept: Optional[ImageConcept] = None,
                      brand_kit: Optional[str] = None) -> ImageBrief:
    """Author the brief for one render. Never raises — degrades to a deterministic brief.

    Args:
        content: The piece the image represents. A ``_EXCERPT_CHARS`` excerpt reaches the author.
        surface: Which ``_STYLE_PRESETS`` use case applies; unknown surfaces take the default.
        ratio: The aspect ratio to compose for.
        profile: The author's LinkedIn profile, for the brand context line.
        avatar: The resolved avatar when the author's likeness is in frame; forces people_scene.
        extra_direction: The author's free-text direction for THIS image (issue #1890).
        content_shape: A short tag such as a newsletter edition's format + hook style (#1992).
        avoid_terms: What the author's recent images already looked like. A SOFT steer to the
            author only — never a gate and never in the fallback, because relevance outranks
            variety and a fallback prompt is read by the renderer as a request.
        concept: Stage 1's analysis, when the caller already has one; otherwise this runs it.
        brand_kit: A pre-rendered brand clause (palette, type), folded into the author's context
            and the graphic fallback's palette.

    Returns:
        The brief. ``fallback`` is True when the deterministic template shipped.
    """
    from cqc_lem.utilities.ai.ai_helper import _call_llm, _loads_json_object, _profile_visual_context
    from cqc_lem.utilities.avatar.attributes import subject_directive

    if concept is None:
        try:
            concept = analyze_content_for_image(content, surface=surface)
        except Exception as e:  # analyze never raises, but the brief must not depend on that
            log_debug("Image concept stage raised — briefing without it", error=str(e),
                      surface=surface, action_type="image_brief")
            concept = None
    preset = surface if surface in _STYLE_PRESETS else _DEFAULT_PRESET
    treatment = _treatment_for(concept, avatar)
    entities = usable_entities(concept)
    weak = concept is None or concept.weak or len(entities) < WEAK_ENTITY_FLOOR
    hook = concept.hook_phrase if concept and treatment == TREATMENT_GRAPHIC else None
    # The likeness directive leads the context on purpose (issue #744): with nothing stating who
    # a depicted person is, the model invents one and the LoRA renders the invention.
    context = _profile_visual_context(profile, subject_directive(avatar))
    avoid = [t.strip() for t in (avoid_terms or []) if t and t.strip()][:_MAX_AVOID_TERMS]

    user_prompt = (
        f"{context}USE CASE: {_STYLE_PRESETS[preset]}\n"
        f"{_TREATMENT_TEMPLATES[treatment]}\n\n"
        f"Compose for a {ratio} aspect ratio.\n"
        + _analysis_block(concept, entities, hook)
        + (f"Brand: {brand_kit}\n" if brand_kit else "")
        + ("" if avatar else _NO_AVATAR_PEOPLE)
        + (f"Content shape: {content_shape}\n" if content_shape else "")
        + (f"This author's recent images already looked like this, so make this one visibly "
           f"different in setting and composition: {'; '.join(avoid)}\n" if avoid else "")
        + (f"Additional direction: {extra_direction}\n" if extra_direction else "")
        + f"\nExcerpt of the piece the image must represent:\n"
          f"<content>{(content or '')[:_EXCERPT_CHARS]}</content>")

    reason = "unknown"
    judged = False
    # A brief that passed every deterministic check but that Stage 3's judge objected to: shipped
    # if the one repair pass cannot do better, because a real brief beats the template.
    judged_brief: Optional[ImageBrief] = None
    for attempt in range(1, _BRIEF_ATTEMPTS + 1):
        try:
            # The retry carries WHY the last attempt was thrown out. Re-sending the identical
            # prompt just re-drew the same rejected scene (issue #1992).
            retry_note = ("" if attempt == 1 else
                          f"\n\nYour previous attempt was REJECTED: {reason}. Write a different "
                          f"brief that fixes exactly that.")
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
            # Tolerant parse: a fenced reply is still a good brief (issues #1323, #2013).
            parsed = _loads_json_object(raw)
            if parsed is None:
                reason = "unparsable JSON (fenced or malformed reply)"
                log_debug("Image brief reply was not valid JSON", surface=surface,
                          attempt=attempt, ai_model="lem-medium", reason=reason)
                continue
            rejection = _rejection(parsed, entities=entities, weak=weak, hook_text=hook)
            if rejection:
                reason = rejection
                log_debug("Image brief failed validation — retrying", surface=surface,
                          attempt=attempt, ai_model="lem-medium", reason=reason)
                continue
            prompt = str(parsed["prompt"]).strip()
            brief = ImageBrief(
                prompt=prompt, ratio=ratio, surface=surface, style_preset=preset,
                focal_concept=str(parsed["focal_concept"]).strip(), fallback=False,
                concept=concept, treatment=treatment,
                required_entities=tuple(e for e in entities if entity_mentioned(e, prompt)),
                hook_text=hook)
            if concept is None or judged:
                brief.prompt_check = (f"repaired after: {reason}" if judged
                                      else "deterministic checks passed")
                return brief
            ok, why = check_prompt_against_concept(prompt, concept, hook)
            brief.prompt_check = why
            if ok or attempt == _BRIEF_ATTEMPTS:
                return brief
            # ONE repair pass through the author with the judge's reason.
            judged, judged_brief, reason = True, brief, why
            log_debug("Image prompt check failed — one repair pass", surface=surface,
                      attempt=attempt, action_type="image_brief", reason=why)
        except Exception as e:
            # One condition, ONE warning: the fallback below carries it — per-attempt noise
            # would double-file the same fault with the escalation cron.
            reason = f"{type(e).__name__}: {e}"[:120]
            log_debug("Image brief attempt failed", error=str(e), ai_model="lem-medium",
                      attempt=attempt)
    if judged_brief is not None:
        # Expected, not a defect: a valid brief the judge doubted, and the repair fell short.
        log_debug("Image brief repair fell short — shipping the judged brief", surface=surface,
                  action_type="image_brief", reason=reason)
        return judged_brief
    # Carry WHY on the warning: this fell back on every generation for weeks and the message
    # alone gave no way to tell an outage from an empty response from a validation reject.
    log_warning("Image brief fell back to the deterministic template", surface=surface,
                action_type="image_brief", reason=reason)
    return _fallback_brief(content, surface=surface, ratio=ratio, context=context, avatar=avatar,
                           concept=concept, treatment=treatment, brand_kit=brand_kit)
