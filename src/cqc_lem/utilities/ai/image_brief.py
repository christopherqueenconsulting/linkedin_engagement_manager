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
    GENERIC_ACRONYMS,
    TREATMENT_CONCRETE,
    TREATMENT_GRAPHIC,
    TREATMENT_METAPHOR,
    TREATMENT_PEOPLE,
    WEAK_ENTITY_FLOOR,
    ImageConcept,
    analyze_content_for_image,
    entity_mentioned,
    is_fact_only,
    name_tokens,
)
from cqc_lem.utilities.logger import log_debug, log_info, log_warning
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
        "TREATMENT people_scene: a photorealistic, candid documentary photograph of the piece's "
        "audience in the exact situation it describes, built from the chosen visual idea. The "
        "emotional beat is VISIBLE as a specific human reaction — a wince at a number, mid-laugh "
        "relief, a raised eyebrow at a draft, arms crossed in a tense meeting — in a close or "
        "medium framing where the face reads at thumbnail size. A laptop is at most a prop. Real "
        "texture: visible skin pores, fabric wear and creases, scuffed surfaces and everyday "
        "imperfections rather than studio polish. When the author's likeness is supplied, the "
        "author is the person in the scene."),
    TREATMENT_GRAPHIC: (
        "TREATMENT editorial_graphic: a designed editorial graphic. The ONLY words in the image "
        "are the hook, in double quotes, spelled exactly, set in bold sans-serif type with its "
        "placement stated (for example large in the left third). One supporting photographic "
        "element drawn from the visual anchors sits beside it on a flat brand-palette color "
        "field with generous negative space."),
    TREATMENT_CONCRETE: (
        "TREATMENT concrete_scene: a photorealistic photograph of the specific, tangible "
        "situation the visual anchors describe, shot where it really happens, with real texture "
        "— worn edges, fingerprints, fabric wear, dust and everyday imperfections rather than "
        "studio polish. Screens and documents may appear, blank or abstract."),
    TREATMENT_METAPHOR: (
        "TREATMENT metaphor_last_resort: one uncommon visual metaphor specific to this piece's "
        "thesis, photographed as a real scene — one a reader has never seen on a hundred other "
        "covers. Stock symbols are refused."),
}

# The ONE vocabulary ban list, named by the system prompt below AND read by the checking side
# (`tests/unit/utilities/ai/test_image_preset_drift.py`) — the writer side and the checking side
# cannot drift, which is exactly how the `thumbnail` preset came to ask for the medium the system
# prompt forbids (issue #1141).
# "photorealistic" was on this list until gauntlet round 1 of #2241: OpenAI's gpt-image guide
# recommends it, paired with real texture (pores, fabric wear, imperfections) and no studio polish —
# https://developers.openai.com/cookbook/examples/multimodal/image-gen-models-prompting-guide
DEAD_QUALITY_TAGS = ("cinematic", "8k", "masterpiece", "ultra-detailed")
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
    # Money and stock business imagery (gauntlet round 1: a stack of $100 bills passed).
    "stack of cash", "pile of cash", "bundle of cash", "wad of cash", "stack of bills",
    "pile of money", "stack of money", "dollar bill", "banknote", "pile of coins",
    "stack of coins", "coin stack", "piggy bank", "money bag", "shaking hands", "thumbs up",
    "hologram", "data visualization hologram", "glowing dashboard", "data stream",
    # Stock office (issue #1992).
    "person at laptop", "man at laptop", "woman at laptop", "professional at laptop",
    "typing hands", "hands typing", "hands on keyboard", "coffee and notebook",
    "calculator with coins",
    # Round 3 (#2241): four covers of people at laptops with neutral faces. A laptop may be a
    # prop; looking at or typing on one is never the action, and a neutral face is no reaction.
    "neutral expression", "looking at laptop", "typing on laptop", "staring at laptop",
    "focused on laptop", "working on laptop", "person typing", "professional typing",
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


def _word_pattern(word: str) -> str:
    forms = _inflections(word) if len(word) >= 3 else {word}
    return "(?:" + "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True)) + ")"


def _phrase_pattern(phrase: str) -> str:
    # Every word inflects ("stacks of cash", "piles of coins"), joined by an optional article.
    return _PHRASE_GAP.join(_word_pattern(w) for w in phrase.lower().split())


_CLICHE_PATTERN = re.compile(
    r"\b(?:" + "|".join(_phrase_pattern(p) for p in sorted(CLICHE_OBJECTS, key=len, reverse=True))
    + r")\b", re.IGNORECASE)
# Stock PAIRINGS: either half alone is an ordinary object; together they are the cliché.
_PAIRINGS: tuple[tuple[str, "re.Pattern[str]", "re.Pattern[str]"], ...] = (
    ("coffee and notebook", re.compile(r"\bcoffee\b", re.I), re.compile(r"\bnotebooks?\b", re.I)),
    ("calculator with coins", re.compile(r"\bcalculators?\b", re.I),
     re.compile(r"\bcoins?\b", re.I)),
)


def cliche_hit(text: Optional[str]) -> Optional[str]:
    """The first stock symbol ``text`` names, or None.

    Args:
        text: A render prompt, an entity, or a vision judge's description.

    Returns:
        The matched words as written (e.g. ``"dripping"``), or the pairing's name (``"coffee and
        notebook"``, ``"calculator with coins"``) when both halves appear anywhere in the text.
    """
    text = text or ""
    for match in _CLICHE_PATTERN.finditer(text):
        word = match.group(0)
        if word.lower().startswith("target") and _TARGET_BUSINESS_SENSE.match(text, match.end()):
            continue
        return word
    for name, first, second in _PAIRINGS:
        if first.search(text) and second.search(text):
            return name
    return None


def _scrub_cliches(text: str) -> str:
    """``text`` with every stock symbol removed — for anything the fallback interpolates."""
    scrubbed = _CLICHE_PATTERN.sub(" ", text or "")
    return " ".join(scrubbed.replace('"', "").replace("“", "").replace("”", "").split())


# Encodes the OpenAI gpt-image and BFL FLUX prompting guides (2026): state the use case, order the
# prompt scene -> subject -> details -> constraints, quote any text exactly with its placement and
# type, and get realism from "photorealistic", camera language and real texture (pores, fabric
# wear, imperfections) rather than studio polish. Positive phrasing only (FLUX has no negative
# prompts — naming a thing summons it).
_SYSTEM_PROMPT_HEAD = """Act as a photo editor and art director writing the brief for ONE image \
of LinkedIn visual content. You receive the use case, a TREATMENT, an analysis of the piece \
(thesis, audience, facts, visual anchors, emotional beat, hook) and an excerpt of the piece \
itself, and you turn them into a single render-ready prompt.

### Write the prompt in this order (earlier = more weight)
1. SCENE: what this image is for and where it takes place, as the use case asks.
2. SUBJECT: who or what carries the frame — built from the piece's VISUAL ANCHORS, never a
   generic stand-in or a symbol. 3. DETAILS: composition for the requested aspect ratio;
   LIGHTING precisely named ("soft window light from camera left with natural fill"); CAMERA,
   one body, one focal length, one aperture ("shot on Sony A7R IV, 35mm f/2"); FINISH with real
   texture and controlled imperfection ("Kodak Portra 400 tones, subtle film grain, visible skin
   pores, fabric wear"). For an editorial graphic: the type weight, the color fields and exactly
   where the hook sits.
4. CONSTRAINTS last.

### Hard rules
- ONE flowing natural-language paragraph of 40-90 words. No keyword stacking, no weight syntax.
- Build the image on the CHOSEN VISUAL IDEA. Depict at least one visual anchor literally, and
  name the ones you used in required_entities.
- When a person appears, the emotional beat shows on their face as a specific reaction readable at
  thumbnail size. A laptop is at most a prop — never the thing a person is looking at or typing on.
- Include ONE deliberate brand-color element, named by its color: the hook's color, a wardrobe
  accent, a wall or background accent, or a warm gold grade.
- The FACTS are context only. NEVER put a company, brand, product, model or report name, or a
  number, in the prompt — a renderer cannot draw a name, it writes it as garbled text or invents
  a prop that "represents" it.
- Nothing in the scene carries words: never "displaying the X dashboard", "titled", "labelled",
  "a sign reading", or a report, magazine or screen "with text". Screens and documents may
  appear, blank or abstract; documents and checklists are blank or unreadable — no numerals, no
  list markers, never "a three-step checklist".
- Stock symbols are REFUSED, whatever the topic: """ + ", ".join(CLICHE_OBJECTS) + """. Find
  what THIS piece shows instead.
- Quotation marks appear ONLY around the hook, copied exactly, when a hook is given (every
  newsletter cover, and the editorial_graphic treatment). Otherwise no quoted text and no hook.
"""

_VOCABULARY_RULES = (
    "- Specificity IS realism. Never lean on generic tags — "
    + ", ".join(DEAD_QUALITY_TAGS)
    + " are dead words; photorealistic plus concrete gear, named lighting and tactile texture "
      "do that work.\n"
    "- Photography and editorial-design vocabulary only. A single word like "
    + ", ".join(DEAD_STYLE_WORDS)
    + " drags the image away from a real photograph or a designed graphic.\n")

_SYSTEM_PROMPT_TAIL = """- The renderer ignores negation, so never write "no X" or "without X" — and NEVER mention
  text, letters, numbers, logos, watermarks, brands, charts, or UI at all, the editorial_graphic
  hook being the one exception: naming them summons them, garbled. Describe surfaces positively
  instead: plain unbranded clothing, clean unmarked walls.
- A SCREEN is where marks appear even when the brief never asked for them: a described laptop,
  monitor or phone invites the renderer to fill its glass with plausible UI, tiled app icons and
  company marks. Use one only when it genuinely belongs, keep its glass blank or abstract, and
  put the weight on the person's reaction and the room around it.
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
 "required_entities": ["<each visual anchor the prompt depicts>"],
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

# Phrases that put WORDS on a surface (gauntlet round 1 of #2241: "a tablet displaying the
# Originality.ai dashboard" came back reading 'Originality ai', a report cover as garbled words).
# A screen or a document may appear; what it may not do is carry named content.
_WORDS_ON_SURFACE = re.compile(
    r"\b(?:titled|entitled|labell?ed|captioned|inscribed|emblazoned)\b"
    r"|\b(?:sign|label|tag|banner|poster|cover|caption|headline|note|card|screen|page)s?\s+"
    r"(?:that\s+)?(?:reads|reading|says|saying)\b"
    r"|\breading\s*[:\"“']"
    r"|\b(?:display(?:s|ing)?|show(?:s|ing))\s+(?:the|a|an|its|their)?\s*(?:[\w.\-]+\s+){0,3}"
    r"(?:dashboard|website|homepage|app|interface|logo|text|words|headline|title|report)\b"
    r"|\b(?:report|magazine|screen|document|page|cover|book|slide)s?\s+with\s+(?:the\s+)?"
    r"(?:text|words|writing|headline|title|caption)s?\b"
    # Round 3: "a printed three-step checklist" came back reading "1, 2, 8".
    r"|\bnumbered\b|\bbullet(?:ed)?\s+(?:points?|list)\b|\blist\s+of\s+numbers\b"
    r"|\b(?:\w+|\d+)[\s\-‑]step\s+(?:checklist|list|guide|plan|process)s?\b",
    re.IGNORECASE)

# Round 3 (#2241): people and concrete scenes lost the palette entirely. A cover brief must name
# one deliberate brand-color element; these are the color words it may name it by.
_COLOR_WORDS = frozenset({
    "gold", "golden", "charcoal", "off-white", "cream", "ivory", "navy", "teal", "amber",
    "ochre", "mustard", "black", "white", "grey", "gray", "green", "blue", "red", "orange",
    "yellow", "purple", "burgundy", "slate", "sand", "beige", "copper", "bronze", "silver",
    "coral", "olive", "forest", "emerald", "crimson", "indigo", "magenta", "pink", "brown"})
_DEFAULT_BRAND_COLORS = frozenset({"gold", "golden", "charcoal", "off-white"})
BRAND_ACCENT_DIRECTIVE = (
    "BRAND ACCENT: include exactly one deliberate brand-color element — the hook's color, a "
    "wardrobe accent, a wall or background accent, or a warm gold grade — named by its color "
    "({colors}).\n")
# Every newsletter cover carries its hook as a headline (round 3): the hook carries the THESIS,
# the visual carries emotion and specificity — the YouTube-thumbnail / editorial-cover pattern.
COVER_HOOK_LAYOUT = (
    'COVER LAYOUT: the headline "{hook}" sits in one third of the frame — left or right, opposite '
    "the subject — in bold geometric sans, brand light gold or off-white on a dark area, five "
    "words at most. The visual subject fills the other two thirds.\n")


def brand_colors(brand_kit: Optional[str]) -> frozenset[str]:
    """The color words a brief may name its brand accent by.

    Args:
        brand_kit: The pre-rendered brand clause, or None.

    Returns:
        The color words the clause names, or the default charcoal / off-white / gold palette.
    """
    found = {w for w in re.findall(r"[a-z][a-z\-]*", (brand_kit or "").lower()) if w in _COLOR_WORDS}
    return frozenset(found) or _DEFAULT_BRAND_COLORS


def _names_a_color(prompt: str, colors: frozenset[str]) -> bool:
    lowered = prompt.lower()
    return any(re.search(rf"\b{re.escape(c)}\b", lowered) for c in colors)


def carries_hook(surface: str, treatment: str) -> bool:
    """Does this image carry the concept's hook? Every newsletter cover, and any graphic."""
    return surface == "newsletter" or treatment == TREATMENT_GRAPHIC


# Name tokens a prompt may carry anyway: the platform the use case itself names.
_ALLOWED_NAME_TOKENS = frozenset({"LinkedIn"})

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
    brief took, `required_entities` the visual anchors the prompt actually depicts, `hook_text`
    the ONE string the render may carry (editorial_graphic only), `prompt_check` what Stage 3
    said, and `rejections` the reason every rejected attempt was thrown out — all recorded on the
    brief receipt (issue #2241), so a fallback says WHY it fell back.
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
    rejections: tuple[str, ...] = ()


def usable_anchors(concept: Optional[ImageConcept]) -> list[str]:
    """The DEPICTABLE things a prompt for this concept must show.

    The concept's ``visual_anchors``; for a concept that has none (an older receipt, or a reply
    that named no anchors), its plain common-noun ``specific_entities`` instead — never a name or a
    number. Stock symbols are dropped either way: a cliché is refused even when the text names it,
    so asking a prompt to depict one would make the brief unpassable. Shared with the vision gate,
    which asks about the same anchors.

    Args:
        concept: Stage 1's analysis, or None.

    Returns:
        The anchors a prompt may be asked to depict; empty without a concept.
    """
    if concept is None:
        return []
    anchors = concept.visual_anchors or tuple(
        e for e in concept.specific_entities if not is_fact_only(e))
    return [a for a in anchors if not cliche_hit(a) and not name_tokens(a)]


def fact_name_tokens(concept: Optional[ImageConcept]) -> set[str]:
    """Every name/number token of the concept's FACTS — what a render prompt must never carry.

    Args:
        concept: Stage 1's analysis, or None.

    Returns:
        Tokens as written (case-sensitive), minus ``_ALLOWED_NAME_TOKENS``.
    """
    tokens: set[str] = set()
    for fact in (concept.specific_entities if concept else ()):
        if is_fact_only(fact):
            tokens.update(name_tokens(fact))
            first = fact.split()[0].strip(".,") if fact.split() else ""
            if first[:1].isupper() and first not in GENERIC_ACRONYMS:
                tokens.add(first)
    return {t for t in tokens if len(t) >= 2 and t not in _ALLOWED_NAME_TOKENS}


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


def _strip_hook(prompt: str, hook_text: Optional[str]) -> str:
    if not hook_text:
        return prompt
    for quote in (f'"{hook_text}"', f"“{hook_text}”"):
        prompt = prompt.replace(quote, " ")
    return prompt


def _named_fact(prompt: str, names: set[str]) -> Optional[str]:
    for name in sorted(names, key=len, reverse=True):
        if re.search(rf"(?<![\w$]){re.escape(name)}(?![\w])", prompt):
            return name
    return None


def _deterministic_failure(prompt: str, *, anchors: list[str], weak: bool,
                           hook_text: Optional[str],
                           names: Optional[set[str]] = None,
                           colors: Optional[frozenset[str]] = None) -> Optional[str]:
    """Why ``prompt`` fails the deterministic checks, or None when it passes them.

    Shared by Stage 2's validation and Stage 3, so the two can never disagree about a cliché,
    a stray quote, a name, words on a surface, or anchor coverage.
    """
    hit = cliche_hit(prompt)
    if hit:
        return (f"the prompt named the stock symbol {hit!r} — build the image on the piece's "
                f"own visual anchors instead")
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
    unhooked = _strip_hook(prompt, hook_text)
    named = _named_fact(unhooked, names or set())
    if named:
        return (f"the prompt names {named!r}, a fact the image cannot draw — show the visual "
                f"anchors, never a name or a number")
    words = _WORDS_ON_SURFACE.search(unhooked)
    if words:
        return (f"the prompt puts words on a surface ({words.group(0).strip()!r}) — screens and "
                f"documents stay blank or abstract")
    # Advisory since round 3: the hook carries the thesis, so ONE anchor in frame is enough — two
    # pushed ed16 onto the fallback twice for "depicts 1 of the anchors".
    if not weak and anchors and not any(entity_mentioned(a, prompt) for a in anchors):
        return (f"the prompt depicts none of the piece's visual anchors; show at least one of: "
                f"{'; '.join(anchors)}")
    if colors and not _names_a_color(unhooked, colors):
        return (f"the prompt has no deliberate brand-color element — name one accent in "
                f"{', '.join(sorted(colors))}")
    return None


def _rejection(parsed: dict[str, Any], *, anchors: list[str], weak: bool,
               hook_text: Optional[str], names: Optional[set[str]] = None,
               colors: Optional[frozenset[str]] = None) -> Optional[str]:
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
        return "the reply declared hook text, but this image carries no hook"
    return _deterministic_failure(prompt, anchors=anchors, weak=weak, hook_text=hook_text,
                                  names=names, colors=colors)


_CHECK_PROMPT = """You are checking an image prompt BEFORE it is sent to a renderer.

The piece's thesis: {thesis}
What the image should show: {anchors}
The image's headline: {hook}

The prompt:
<prompt>{prompt}</prompt>

Together with its headline, would an image rendered from this prompt let a stranger guess the
piece's thesis? Name which of the things above the prompt depicts. Respond with ONLY a JSON object:
{{"guessable": true|false, "depicted_entities": ["..."], "reason": "<one short sentence>"}}"""


def _concept_is_weak(concept: Optional[ImageConcept], anchors: list[str]) -> bool:
    return concept is None or concept.weak or len(anchors) < WEAK_ENTITY_FLOOR


@llm_step("image_prompt_check")
def check_prompt_against_concept(prompt: str, concept: Optional[ImageConcept],
                                 hook_text: Optional[str]) -> tuple[bool, str]:
    """Stage 3: is this prompt worth rendering? Deterministic checks first, then ONE judge call.

    The deterministic half (stock symbols, stray quoted text, fact names, words on a surface,
    anchor coverage) always applies. The ``lem-simple`` judge then asks whether a stranger could
    guess the thesis from the image; it fails OPEN — an unreachable judge passes the prompt, since
    the vision gate still stands behind.

    Args:
        prompt: The authored render prompt.
        concept: Stage 1's analysis; without one only the deterministic checks run.
        hook_text: The one string the image may carry, or None.

    Returns:
        ``(ok, reason)`` — ``reason`` is the repair directive when ``ok`` is False.
    """
    anchors = usable_anchors(concept)
    failure = _deterministic_failure(prompt, anchors=anchors,
                                     weak=_concept_is_weak(concept, anchors),
                                     hook_text=hook_text, names=fact_name_tokens(concept))
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
                thesis=concept.thesis, anchors="; ".join(anchors) or "(none named)",
                hook=f'"{hook_text}"' if hook_text else "(none)", prompt=prompt)}],
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


def _plain_words(text: str) -> str:
    """``text`` with stock symbols, quotes, names and numbers removed — safe to interpolate."""
    words = _scrub_cliches(text).split()
    # Interpolated mid-sentence, so ANY capitalised word is a name (or a sentence start, which is
    # cheap to lose) — only the generic acronyms survive.
    kept = [w for w in words
            if not name_tokens(w) and not (w[:1].isupper() and w.strip(".,;:") not in
                                           GENERIC_ACRONYMS)
            and not any(ch in w for ch in "$%€£")]
    return " ".join(kept)


def _fallback_treatment(treatment: str, anchors: list[str]) -> str:
    """The fallback's treatment: a photograph, never a graphic.

    The deterministic graphic once rendered "a photographic cutout of $30K" (gauntlet round 1 of
    #2241). A people_scene stays one; anything else is a concrete scene when there are anchors.
    """
    if treatment in (TREATMENT_PEOPLE, TREATMENT_CONCRETE):
        return treatment
    return TREATMENT_CONCRETE if anchors else TREATMENT_PEOPLE


def _fallback_brief(content: str, *, surface: str, ratio: str, context: str,
                    avatar: Optional[dict[str, Any]] = None,
                    concept: Optional[ImageConcept] = None,
                    treatment: Optional[str] = None,
                    rejections: tuple[str, ...] = (),
                    brand_kit: Optional[str] = None) -> ImageBrief:
    """Deterministic last resort when the brief author is down — built from the piece, not a set.

    Every noun here is a RENDER request, so it is assembled only from the concept's visual
    anchors (never its facts), as a people_scene or concrete_scene photograph, with every stock
    symbol, name and number scrubbed out of anything interpolated. The old newsletter fallback was
    a workshop still-life (a brass valve, a gauge, a gear) — the scene issue #2241 was filed about
    — and the old graphic fallback put a number in as its subject; both are gone.

    Since round 3 it builds from the concept's top-ranked VISUAL IDEA when there is one — never a
    bare anchor list — and a newsletter cover keeps its hook as the headline, laid out like the
    authored ones. The brand accent is named by color only; the brand clause itself never reaches
    a render prompt.
    """
    treatment = _fallback_treatment(treatment or _treatment_for(concept, avatar),
                                    usable_anchors(concept))
    anchors = [_plain_words(a) for a in usable_anchors(concept)]
    anchors = [a for a in anchors if a]
    use = _USE_LABELS.get(surface, _USE_LABELS[_DEFAULT_PRESET])
    summary = _plain_words(concept.thesis if concept else content)[:240]
    hook = (concept.hook_phrase if concept and concept.hook_phrase
            and carries_hook(surface, treatment) else None)
    accent = sorted(brand_colors(brand_kit) & {"gold", "golden"}) or sorted(brand_colors(brand_kit))
    finish = (f"composed for a {ratio} aspect ratio with the subject large in the frame, soft "
              f"window light with natural fill and one deliberate {accent[0]} accent, subtle film "
              f"grain, real texture with fabric wear and everyday imperfections, plain unbranded "
              f"surfaces, clean unmarked walls.")
    layout = (f' The headline "{hook}" sits in the left third in bold geometric sans, light gold '
              f"on a dark area; the subject fills the other two thirds." if hook else "")
    idea = _plain_words(concept.chosen_idea) if concept and concept.chosen_idea else ""

    if idea:
        prompt = (f"{context}A photorealistic editorial photograph for a {use}: {idea}, shot on a "
                  f"50mm lens at f/2.8, visible skin pores, {finish}{layout}")
    elif treatment == TREATMENT_PEOPLE:
        who = _plain_words(concept.audience) if concept and concept.audience else ""
        who = who or "a small working team"
        around = _join(anchors[:3]) if anchors else f"the situation this describes: {summary}"
        prompt = (f"{context}A photorealistic candid documentary photograph for a {use}: {who} "
                  f"caught mid-conversation around {around}, in a real working setting, "
                  f"eye-level medium shot, shot on a 35mm lens at f/2, visible skin pores, plain "
                  f"unbranded clothing, {finish}{layout}")
    elif len(anchors) >= 2:
        extra = f", with {anchors[2]} in the frame" if len(anchors) > 2 else ""
        prompt = (f"{context}A photorealistic candid editorial photograph for a {use} of "
                  f"{anchors[0]} and {anchors[1]} in the real place this happens{extra}, shot on "
                  f"a 50mm lens at f/2.8, {finish}{layout}")
    else:
        subject = anchors[0] if anchors else f"the real, specific situation this describes: {summary}"
        prompt = (f"{context}A photorealistic candid editorial photograph for a {use} of "
                  f"{subject}. One clear focal subject, shot on a 50mm lens at f/2.8, "
                  f"{finish}{layout}")
    return ImageBrief(prompt=prompt, ratio=ratio, surface=surface,
                      style_preset=surface if surface in _STYLE_PRESETS else _DEFAULT_PRESET,
                      focal_concept=(summary[:120] or "professional LinkedIn visual"),
                      fallback=True, concept=concept, treatment=treatment,
                      required_entities=tuple(a for a in anchors if entity_mentioned(a, prompt)),
                      hook_text=hook, rejections=tuple(rejections))


def _analysis_block(concept: Optional[ImageConcept], anchors: list[str],
                    hook: Optional[str], surface: str = "post_image") -> str:
    if concept is None:
        return ("Analysis of the piece: unavailable — read the excerpt and build the image on "
                "the specific, depictable things it describes.\n")
    analysis = {"thesis": concept.thesis, "audience": concept.audience,
                "facts_context_only_never_drawn": list(concept.specific_entities),
                "visual_anchors_to_depict": anchors,
                "emotional_beat_to_show": concept.emotional_beat}
    block = f"Analysis of the piece: {json.dumps(analysis)}\n"
    if concept.chosen_idea:
        block += f"CHOSEN VISUAL IDEA — build the image on this: {concept.chosen_idea}\n"
    if hook:
        block += f'Hook — the ONLY text in the image, quoted exactly: "{hook}"\n'
        if surface == "newsletter" and concept.treatment != TREATMENT_GRAPHIC:
            block += COVER_HOOK_LAYOUT.format(hook=hook)
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
        brand_kit: A pre-rendered brand clause (palette, type), folded into the AUTHOR's context
            only — never pasted into the fallback, which is a render prompt.

    Returns:
        The brief. ``fallback`` is True when the deterministic template shipped; ``rejections``
        carries why every rejected attempt was thrown out.
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
    anchors = usable_anchors(concept)
    weak = _concept_is_weak(concept, anchors)
    names = fact_name_tokens(concept)
    hook = (concept.hook_phrase if concept and concept.hook_phrase
            and carries_hook(surface, treatment) else None)
    colors = brand_colors(brand_kit)
    # Deterministically enforced on covers; requested on every surface.
    gate_colors = colors if surface == "newsletter" else None
    # The likeness directive leads the context on purpose (issue #744): with nothing stating who
    # a depicted person is, the model invents one and the LoRA renders the invention.
    context = _profile_visual_context(profile, subject_directive(avatar))
    avoid = [t.strip() for t in (avoid_terms or []) if t and t.strip()][:_MAX_AVOID_TERMS]

    user_prompt = (
        f"{context}USE CASE: {_STYLE_PRESETS[preset]}\n"
        f"{_TREATMENT_TEMPLATES[treatment]}\n\n"
        f"Compose for a {ratio} aspect ratio.\n"
        + _analysis_block(concept, anchors, hook, surface)
        + (f"Brand: {brand_kit}\n" if brand_kit else "")
        + BRAND_ACCENT_DIRECTIVE.format(colors=", ".join(sorted(colors)))
        + ("" if avatar else _NO_AVATAR_PEOPLE)
        + (f"Content shape: {content_shape}\n" if content_shape else "")
        + (f"This author's recent images already looked like this, so make this one visibly "
           f"different in setting and composition: {'; '.join(avoid)}\n" if avoid else "")
        + (f"Additional direction: {extra_direction}\n" if extra_direction else "")
        + f"\nExcerpt of the piece the image must represent:\n"
          f"<content>{(content or '')[:_EXCERPT_CHARS]}</content>")

    reason = "unknown"
    rejections: list[str] = []
    # Attempts where the AUTHOR never answered usably (exception, empty, unparsable) — as opposed
    # to answering with a brief the checks refused. Only an all-outage fallback is a warning.
    outages = 0
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
                rejections.append(reason)
                outages += 1
                log_debug("Image brief came back empty", surface=surface, attempt=attempt,
                          ai_model="lem-medium", reason=reason)
                continue
            # Tolerant parse: a fenced reply is still a good brief (issues #1323, #2013).
            parsed = _loads_json_object(raw)
            if parsed is None:
                reason = "unparsable JSON (fenced or malformed reply)"
                rejections.append(reason)
                outages += 1
                log_debug("Image brief reply was not valid JSON", surface=surface,
                          attempt=attempt, ai_model="lem-medium", reason=reason)
                continue
            rejection = _rejection(parsed, anchors=anchors, weak=weak, hook_text=hook,
                                   names=names, colors=gate_colors)
            if rejection:
                reason = rejection
                rejections.append(reason)
                log_debug("Image brief failed validation — retrying", surface=surface,
                          attempt=attempt, ai_model="lem-medium", reason=reason)
                continue
            prompt = str(parsed["prompt"]).strip()
            brief = ImageBrief(
                prompt=prompt, ratio=ratio, surface=surface, style_preset=preset,
                focal_concept=str(parsed["focal_concept"]).strip(), fallback=False,
                concept=concept, treatment=treatment,
                required_entities=tuple(a for a in anchors if entity_mentioned(a, prompt)),
                hook_text=hook, rejections=tuple(rejections))
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
            rejections.append(why)
            log_debug("Image prompt check failed — one repair pass", surface=surface,
                      attempt=attempt, action_type="image_brief", reason=why)
        except Exception as e:
            # One condition, ONE warning: the fallback below carries it — per-attempt noise
            # would double-file the same fault with the escalation cron.
            reason = f"{type(e).__name__}: {e}"[:120]
            rejections.append(reason)
            outages += 1
            log_debug("Image brief attempt failed", error=str(e), ai_model="lem-medium",
                      attempt=attempt)
    if judged_brief is not None:
        # Expected, not a defect: a valid brief the judge doubted, and the repair fell short.
        log_debug("Image brief repair fell short — shipping the judged brief", surface=surface,
                  action_type="image_brief", reason=reason)
        judged_brief.rejections = tuple(rejections)
        return judged_brief
    # WHY it fell back, every attempt's reason, at INFO: a validation fallback is the engine
    # working, not a fault. Only an author that never answered usably at all — every attempt an
    # exception, an empty reply or unparsable JSON — is an outage, and that alone is a warning.
    log_info("Image brief fell back to the deterministic template", surface=surface,
             action_type="image_brief", reason=reason, rejections=" | ".join(rejections))
    if outages == _BRIEF_ATTEMPTS:
        log_warning("Image brief author unavailable — deterministic template shipped",
                    surface=surface, action_type="image_brief", reason=reason)
    return _fallback_brief(content, surface=surface, ratio=ratio, context=context, avatar=avatar,
                           concept=concept, treatment=treatment, rejections=tuple(rejections),
                           brand_kit=brand_kit)
