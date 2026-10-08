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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Optional

from cqc_lem.utilities.ai.image_concept import (
    _HOOK_FREE_WORDS as _FRAMING_FREE_WORDS,
    _STOPWORDS as _FRAMING_STOPWORDS,
    ART_STYLES,
    GENERIC_ACRONYMS,
    REASONING_EFFORT,
    TREATMENT_CONCRETE,
    TREATMENT_EDITORIAL,
    TREATMENT_GRAPHIC,
    TREATMENT_METAPHOR,
    TREATMENT_PEOPLE,
    WEAK_ENTITY_FLOOR,
    ImageConcept,
    analyze_content_for_image,
    assign_layout_and_cast,
    cast_phrase,
    concept_kicker,
    entity_mentioned,
    fit_hook,
    is_fact_only,
    name_tokens,
)
from cqc_lem.utilities.logger import log_debug, log_info, log_warning
from cqc_lem.utilities.observability import llm_step


class _NotAnalyzed:
    """Type of ``NOT_ANALYZED`` — "the caller never ran Stage 1", as distinct from "it returned None"."""

    def __repr__(self) -> str:
        return "NOT_ANALYZED"


# The default for ``build_image_brief(concept=)``. A caller that ran Stage 1 passes whatever it
# got, None included, so a failed analysis is never paid for twice on one artifact.
NOT_ANALYZED = _NotAnalyzed()


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
        "smile, a raised eyebrow at a colleague, arms crossed in a tense meeting — in a close or "
        "medium framing where the face reads at thumbnail size — clearly readable but authentic "
        "and restrained, the face a real person would make, never cartoonish or crying. There is "
        "no laptop or screen in frame. The emotion's direction follows the piece's valence: a saving or a "
        "win is a relaxed, genuine smile, eyes bright, shoulders loose, engaged with the other "
        "person or the task, hands away from head and face; a risk or a "
        "loss reads concerned, skeptical or frustrated — never shock or despair for good news. "
        "The image is BRIGHT — high-key or warm daylight with strong subject contrast — never a "
        "dark, moody, low-key scene unless the emotional beat itself is dark (dread, crisis). Real "
        "texture: visible skin pores, fabric wear and creases, scuffed surfaces and everyday "
        "imperfections rather than studio polish. When the author's likeness is supplied, the "
        "author is the person in the scene."),
    TREATMENT_GRAPHIC: (
        "TREATMENT editorial_graphic: a designed editorial layout — one supporting photographic "
        "element drawn from the visual anchors on a flat brand-palette color field with generous, "
        "calm negative space. The headline is typeset onto that space later by the system, so "
        "the image itself carries no words at all."),
    TREATMENT_CONCRETE: (
        "TREATMENT concrete_scene: a photorealistic photograph of the specific, tangible "
        "situation the visual anchors describe, shot where it really happens, with real texture "
        "— worn edges, fingerprints, fabric wear, dust and everyday imperfections rather than "
        "studio polish. The image is BRIGHT — high-key or warm daylight with strong subject "
        "contrast — never a dark, moody, low-key still life. Screens and documents may appear, "
        "blank or abstract."),
    TREATMENT_METAPHOR: (
        "TREATMENT metaphor_last_resort: one uncommon visual metaphor specific to this piece's "
        "thesis, photographed as a real scene — one a reader has never seen on a hundred other "
        "covers. Stock symbols are refused."),
    # Archetype round (#2241, docs/visual-archetypes-research.md §6.2 F): the image carries the
    # IDEA, as an Economist cover does — one juxtaposition of the reader's own everyday objects.
    TREATMENT_EDITORIAL: (
        "TREATMENT editorial_concept: ONE surprising but instantly legible idea built exactly on "
        "the CHOSEN VISUAL IDEA — a juxtaposition, a scale shift or an object contradicting its "
        "own function, made from everyday objects in the reader's own working world. Objects "
        "only: the frame holds no person, face, hand or silhouette. One focal point and ONE "
        "oddity a stranger resolves within two seconds, centred on a flat, calm ground with "
        "generous space around it. Its finish: {style}."),
}
# The fallback style when a concept carries none.
_DEFAULT_ART_STYLE = "editorial_photo"
# Archetype round: an editorial concept shows nobody — refused deterministically, not requested.
# #2241 showcase: a cover chose editorial_concept and shipped a woman on the phone — the role
# nouns ("CFO", "engineering manager") and portrait words were never refused, and a role reads
# to a renderer as a person to draw.
_PERSON_WORDS = re.compile(
    r"\b(?:person|people|man|men|woman|women|face|faces|hand|hands|finger|fingers|figure|"
    r"figures|silhouette|silhouettes|worker|workers|employee|employees|owner|founder|"
    r"customer|customers|client|clients|team|crowd|he|she|his|her|their|portrait|selfie|"
    r"smile|smiling|ceo|cfo|cto|coo|cmo|leader|leaders|manager|managers|engineer|engineers|"
    r"developer|developers|marketer|marketers|executive|executives|officer|officers|"
    r"colleague|colleagues|boss|staff|buyer|buyers|consultant|consultants|audience)\b",
    re.IGNORECASE)


def person_word(text: Optional[str]) -> Optional[str]:
    """The first word in ``text`` that puts a person in frame, or None.

    The deterministic refusal an ``editorial_concept`` prompt must pass — authored, fallback or
    repaired alike — so the archetype renders OBJECT-ONLY.

    Args:
        text: A render prompt or a fragment of one.

    Returns:
        The word as written, or None.
    """
    match = _PERSON_WORDS.search(text or "")
    return match.group(0) if match else None


def editorial_objects(concept: Optional[ImageConcept]) -> list[str]:
    """The object nouns an editorial concept may be built from: no person, name, number or cliché.

    Stage 1's ``idea_nouns`` (the Idea Miner's concrete nouns), then the usable anchors, each kept
    only when it names no person (``person_word``) and survives ``_plain_words``.

    Args:
        concept: Stage 1's concept.

    Returns:
        Distinct object phrases, in order.
    """
    if concept is None:
        return []
    out: list[str] = []
    for raw in tuple(concept.idea_nouns or ()) + tuple(usable_anchors(concept)):
        noun = _plain_words(raw)
        if (noun and not person_word(noun) and not prop_failure(noun) and not tech_hardware(noun)
                and noun.lower() not in (o.lower() for o in out)):
            out.append(noun)
    return out


def treatment_text(treatment: str, concept: Optional[ImageConcept] = None) -> str:
    """The treatment block the author reads, with an editorial concept's rotated art style.

    Args:
        treatment: One of the ``_TREATMENT_TEMPLATES`` keys.
        concept: Stage 1's concept, for its ``art_style``.

    Returns:
        The template text.
    """
    style = ART_STYLES.get(getattr(concept, "art_style", "") or "",
                           ART_STYLES[_DEFAULT_ART_STYLE])
    return _TREATMENT_TEMPLATES[treatment].format(style=style)

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
    # #2241 showcase B: an editorial concept rendered a row of human brains around an alarm
    # clock — the glowing brain's cousin.
    "brain", "brains", "human brain", "brain model",
    "glowing neural network", "circuit board", "binary code", "matrix code", "rocket", "target",
    "dartboard", "bullseye", "mountain summit", "mountain peak", "compass", "hourglass", "domino",
    "chain link", "maze", "ladder", "key and lock", "lock and key", "padlock", "crystal ball",
    "magnifying glass",
    # Money and stock business imagery (gauntlet round 1: a stack of $100 bills passed).
    "stack of cash", "pile of cash", "bundle of cash", "wad of cash", "stack of bills",
    "pile of money", "stack of money", "dollar bill", "banknote", "pile of coins",
    "stack of coins", "coin stack", "piggy bank", "money bag", "shaking hands", "thumbs up",
    "hologram", "data visualization hologram", "glowing dashboard", "data stream",
    # Round 8: "the soft blue glow of a server room" reached a post render, racks and all.
    "server room", "server rack", "server cabinet", "servers", "racks", "rack of servers",
    "data center", "data centre",
    # Round 9: a video frame turned a safe dial and touched a wall switch — security as hardware.
    # "safe" alone is an everyday adjective ("feels safe"), so the object is matched by phrase.
    "safe dial", "wall safe", "steel safe", "office safe", "safe door", "vault", "combination lock",
    "padlock", "dial", "light switch", "toggle switch", "lever",
    # Showcase round 8: cover_17 filled an engine block with props — the owner's original complaint
    # was pipes, gears and water, and an engine is the same machine-as-metaphor.
    "engine block", "car engine", "engine bay", "motor", "piston", "crankshaft", "flywheel",
    "turbine", "gearbox", "sprocket", "clockwork", "machinery", "conveyor belt",
    # Stock office (issue #1992).
    "person at laptop", "man at laptop", "woman at laptop", "professional at laptop",
    "typing hands", "hands typing", "hands on keyboard", "coffee and notebook",
    "calculator with coins",
    # Round 3 (#2241): four covers of people at laptops with neutral faces. A laptop may be a
    # prop; looking at or typing on one is never the action, and a neutral face is no reaction.
    "neutral expression", "looking at laptop", "typing on laptop", "staring at laptop",
    "focused on laptop", "working on laptop", "person typing", "professional typing",
    # Post-image gauntlet on #2249: every one of these came back from a text post's render.
    "server rack", "data center aisle", "datacenter aisle", "data centre aisle",
    "blinking server lights", "server lights", "wall of monitors", "stock trading chart",
    "trading chart screen",
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
  list markers, never "a three-step checklist". Any paper, chart, report, invoice, dashboard or
  screen you name is described as blank, seen at a steep angle so no writing is legible, or out of
  focus.
- Stock symbols are REFUSED, whatever the topic: """ + ", ".join(CLICHE_OBJECTS) + """. Find
  what THIS piece shows instead.
- The image carries NO text at all, ever: any headline is typeset onto it later by the
  system. Never quote the headline or any other words in the prompt.
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
  text, letters, numbers, logos, watermarks, brands, charts, or UI at all: naming them summons
  them, garbled. Describe surfaces positively
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
  relaxed at the sides, hands resting flat on a desk, framed above the waist, hands empty
  (never a mug, cup or coffee — that is stock monotony). Never: pointing at the camera,
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
# The author's likeness IS in frame (post-image gauntlet on #2249). The LoRA path is FLUX, which
# follows a prompt loosely and largely ignores negation, so the defaults it drifts to — a posed
# smile at the lens, a mug on a desk, a busy screen — are steered out POSITIVELY here and refused
# by `_AVATAR_POSE`. The beat comes from Stage 1, so the expression matches the piece.
_AVATAR_PEOPLE = (
    "The author IS in this image, caught mid-action inside the situation, never posing: their "
    "expression and what their hands are doing show {beat}. Their gaze is on the work or the "
    "other people in the scene, the camera observing from the side as a documentary photographer "
    "would. The surfaces around them carry only the props the visual anchors name. Any screen in "
    "frame is dark, blank or softly out of focus.\n")
_AVATAR_DEFAULT_BEAT = "a specific, genuine reaction to what is happening"
# Refused on an avatar brief only: what FLUX turns into a stock headshot.
_AVATAR_POSE = re.compile(
    r"\b(?:(?:looking|smiling|staring|gazing)\s+(?:directly\s+)?(?:at|into|toward)\s+(?:the\s+)?"
    r"(?:camera|lens|viewer)|eye\s+contact|faces?\s+the\s+camera|facing\s+the\s+camera|"
    r"pos(?:ed|ing|es)\b|headshot|portrait\s+of|coffee\s+mug|mug\s+of\s+coffee|cup\s+of\s+coffee)",
    re.IGNORECASE)


def avatar_directive(concept: Optional[ImageConcept]) -> str:
    """The author-in-frame directive for an avatar brief, carrying the concept's emotional beat.

    Args:
        concept: Stage 1's analysis, or None.

    Returns:
        The directive, its beat taken from the concept (a generic genuine reaction without one).
    """
    beat = (concept.emotional_beat.strip() if concept and concept.emotional_beat else "")
    return _AVATAR_PEOPLE.format(beat=beat or _AVATAR_DEFAULT_BEAT)


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
    r"|\b(?:\w+|\d+)[\s\-‑]step\s+(?:checklist|list|guide|plan|process)s?\b"
    # Round 4: the fallback asked for "a handwritten note urging personal storytelling".
    r"|\bhandwritten\s+(?:note|message|sign|list)s?\b"
    r"|\b(?:note|sign|card|whiteboard|board)s?\s+(?:urging|asking|telling|announcing)\b",
    re.IGNORECASE)

# Round 4 (#2241): ed16 asked for "a printed billing dashboard screen" and "a paper check" and the
# chart paper came back reading "MEITE TCHIREUM". Any paper or screen the brief names must say
# HOW it stays unreadable; these name the document, the qualifiers name the how.
_LEGIBLE_DOCUMENT = re.compile(
    r"\bprinted\s+(?:[\w\-]+\s+){0,2}dashboards?\b"
    r"|\bcharts?\s+with\s+(?:[\w\-]+\s+)?(?:labels?|numbers|figures|axes|values)\b"
    r"|\b(?:paper|bank|signed|printed|personal)\s+(?:check|cheque)s?\b|\bcheques?\b"
    r"|\binvoices?\s+(?:showing|displaying|listing|itemi[sz]ing|reading|with)\b",
    re.IGNORECASE)
_DOCUMENT_NOUNS = re.compile(
    r"\b(?:paper|papers|chart|charts|report|reports|invoice|invoices|dashboard|dashboards|"
    r"screen|screens|monitor|monitors|printout|printouts|document|documents|draft|drafts|"
    r"spreadsheet|spreadsheets|statement|statements|receipt|receipts|page|pages|checklist|"
    r"checklists|flip[\s\-]chart|whiteboard|slide|slides)\b", re.IGNORECASE)
_UNREADABLE_QUALIFIERS = re.compile(
    r"\bblank\b|\bsteep\s+angle\b|\bno\s+writing\s+is\s+legible\b|\bout\s+of\s+focus\b|"
    r"\billegible\b|\bunreadable\b|\bface[\s\-]down\b|\bturned\s+away\b|\bsoft[\s\-]focus\b|"
    r"\bfacing\s+away\b|\bback\s+of\s+(?:a|an|the|her|his|their)\b",
    re.IGNORECASE)
DOCUMENT_BLANKING = ("every paper and screen in the frame is blank, seen at a steep angle so no "
                     "writing is legible, or out of focus")

# Round 7 (#2241): stray text on PROPS persisted on every surface even under "blank" — "Proposal"
# on a clipboard, "COST REDUCTION CHECKLIST", "SOURCE PACK", "KPI Dashboard", console.log on a
# monitor, "Name / Date" on a form. The renderer ignores "blank", so the props are refused
# outright, on EVERY surface: paper of any kind, whiteboard or chart content, code, and any screen
# that is not explicitly seen from behind or turned away.
_PAPER_PROPS = re.compile(
    r"\b(?:paper|papers|sheet|sheets|document|documents|report|reports|proposal|proposals|"
    r"clipboard|clipboards|checklist|checklists|form|forms|invoice|invoices|bill|bills|folder|"
    r"folders|binder|binders|printout|printouts|printed|statement|statements|memo|memos|page|"
    r"pages|receipt|receipts|notebook|notebooks|draft|drafts|manuscript|letter|letters|"
    r"whiteboard|whiteboards|chart|charts|graph|graphs|flip[\s\-]chart|code|console|terminal|"
    r"spreadsheet|spreadsheets|sticky\s+notes?|post-it|contract|contracts|agreement|agreements|"
    r"whitepaper|whitepapers|white\s+paper|brochure|brochures|magazine|magazines|"
    # Round 9: gpt-image paints role and group nouns as captions — "Founders & Content Teams In
    # Conversation" on a poster, "SECURITY" on a badge — so anything that carries a label goes.
    r"poster|posters|signage|signboard|signboards|placard|placards|banner|banners|badge|badges|"
    r"lanyard|lanyards|label|labels|nameplate|nameplates|caption|captions)\b", re.IGNORECASE)
# Round 9: a capitalised group caption ("Founders & Content Teams", "Engineering and Security")
# or a group-noun label ("founders and content teams") is exactly what the renderer paints onto
# a poster or a badge. People are described by appearance and action instead.
_GROUP_LABEL = re.compile(
    r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+(?:&|and)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b|"
    r"\b\w+\s*&\s*\w+\b|"
    r"\b\w+\s+and\s+\w+\s+(?:teams|groups|departments|staff|leads|leaders|crews)\b")
# Round 8: gpt-4.1 flagged "person at laptop" on most scenes, so screens are gone entirely —
# laptops, monitors, keyboards, tablets, displays, computers — on every surface. A phone may
# appear only face-down.
_SCREEN_WORDS = re.compile(
    r"\b(?:laptop|laptops|screen|screens|monitor|monitors|tablet|tablets|keyboard|keyboards|"
    r"display|displays|computer|computers|ipad|ipads|desktop|desktops|macbook|macbooks|"
    r"dashboard|dashboards)\b",
    re.IGNORECASE)
_PHONE_WORDS = re.compile(r"\b(?:phone|phones|smartphone|smartphones)\b", re.IGNORECASE)
_FACE_DOWN = re.compile(r"\bface[\s\-]down\b", re.IGNORECASE)
PAPER_FORBID = "forbid"
PROPS_DIRECTIVE = (
    "PROPS: no paper of any kind in frame — no documents, sheets, reports, proposals, "
    "clipboards, checklists, forms, invoices, bills, folders or binders — no whiteboard, chart "
    "or code content — and no screens at all: no laptop, monitor, keyboard, tablet or computer. "
    "No mugs, cups, coffee or tea. Hands are empty, gesturing, or a phone held face-down. "
    "Tell the story with people "
    "interacting and gesturing in a real environment — a warehouse, a shop floor, a meeting room, "
    "a hallway, a kitchen table, a workshop. Describe each person by APPEARANCE and ACTION (\"a "
    "woman in a green jumper pointing at the shelf\"), never by a role or group caption such as "
    "\"founders and content teams\" or \"engineering lead\" — the renderer paints those onto "
    "posters and badges. No posters, signage, name badges, lanyards or labels.\n")
# Round 12: video 101's foreground hand smeared in frame 5 — Runway animates what is nearest the
# lens worst. ``NO_NEAR_FOREGROUND`` is the clause the motion prompt (``MOTION_DISCIPLINE``,
# PR #2249) appends too.
NO_NEAR_FOREGROUND = "no hands or objects close to the camera"
# #2241 showcase: two of three video frames failed text_accuracy on a clock face's numerals, an
# "O" and a "pause" button label, and fell back to Pexels; frames also lacked any brand mark. So a
# frame carries no clock, sign or labelled button at all, and ONE gold or charcoal accent object
# or wardrobe piece — enforced deterministically (``video_frame_failure``, ``VIDEO_ACCENT_COLORS``).
VIDEO_FRAME_DIRECTIVE = (f"VIDEO FRAME: {NO_NEAR_FOREGROUND} — people at mid-distance, hands "
                         f"well away from the lens, nothing in the near foreground. No clocks, no "
                         f"signs, no labelled buttons or keypads anywhere in frame. Include ONE "
                         f"gold or charcoal accent object or wardrobe piece, named by its colour.\n")
VIDEO_ACCENT_COLORS = frozenset({"gold", "golden", "charcoal"})
_VIDEO_TEXT_PROPS = re.compile(
    r"\b(?:clocks?|wall[\s\-]clock|wristwatch(?:es)?|watch[\s\-]face|signage|signboards?|"
    r"(?:street|wall|door|neon|shop|office|exit|road)\s+signs?|buttons?(?![\s\-]*(?:down|up)\b)|keypads?|"
    r"control\s+panels?|remote\s+controls?)\b", re.IGNORECASE)


_LIGHT_NEUTRALS = frozenset({"off-white", "white", "cream", "ivory", "beige", "sand"})


def video_accent_colors(colors: frozenset[str]) -> frozenset[str]:
    """The colour words a video frame's accent may be named by: the kit's accents, never a neutral.

    Args:
        colors: ``brand_colors`` of the brief's brand clause.

    Returns:
        Gold/charcoal when the kit names them; else the kit's non-light colours; else gold and
        charcoal (the reference brand).
    """
    return (colors & VIDEO_ACCENT_COLORS) or frozenset(colors - _LIGHT_NEUTRALS) \
        or VIDEO_ACCENT_COLORS


def video_frame_failure(prompt: Optional[str]) -> Optional[str]:
    """Why a video frame prompt names a prop the judge reads as stray text, or None.

    Args:
        prompt: The authored render prompt.

    Returns:
        The refusal sent back to the author, or None when the prompt is clean.
    """
    match = _VIDEO_TEXT_PROPS.search(prompt or "")
    if not match:
        return None
    return (f"the frame names {match.group(0)!r} — clocks, signs and buttons carry numerals and "
            f"labels the text check fails; show the moment without them")
# Kept for the call sites that name it: covers and posts were the first surfaces with the rule.
COVER_PROPS_DIRECTIVE = {PAPER_FORBID: PROPS_DIRECTIVE}
# An editorial concept keeps every prop rule and drops the people: the objects carry the idea.
EDITORIAL_PROPS_DIRECTIVE = (
    "PROPS: no paper of any kind in frame — no documents, sheets, reports, invoices, bills, "
    "folders or binders — no whiteboard, chart or code content — and no screens at all. OBJECTS "
    "ONLY: no person, face, hand or silhouette anywhere in the frame; the idea is carried by "
    "everyday objects from the reader's working world. No posters, signage, badges or labels.\n")
# Archetype round: a people scene's person looks TOWARD the typeset headline, never the camera —
# gaze cueing pulls the reader's eye to what the person looks at (research §1.2).
_GAZE_TOWARD_PANEL = {
    "split_left": "toward the LEFT edge of the frame",
    "split_right": "toward the RIGHT edge of the frame",
    "split_top": "up and across toward the TOP of the frame",
    "split_bottom": "down and across toward the BOTTOM of the frame",
}


def gaze_directive(concept: Optional[ImageConcept]) -> str:
    """Where a people scene's person looks: toward the headline panel's side, never the lens.

    Args:
        concept: Stage 1's concept.

    Returns:
        The directive line, or '' when the concept is no people scene on a split layout.
    """
    if concept is None or concept.treatment != TREATMENT_PEOPLE:
        return ""
    toward = _GAZE_TOWARD_PANEL.get(concept.layout or "")
    if not toward:
        return ""
    return (f"GAZE: the person's eyes and shoulders turn {toward}, where the headline sits — "
            f"never toward the camera.\n")


def cover_paper_rule(concept: Optional[ImageConcept] = None) -> str:
    """The prop rule for any surface: ``forbid`` since round 7, whatever the idea is about.

    Args:
        concept: Unused — the edge-on exception for document ideas is gone.

    Returns:
        ``PAPER_FORBID``.
    """
    _ = concept
    return PAPER_FORBID


# Round 11 (#2241): an anchor naming tech hardware ("smaller model server") is filtered before
# it can reach a brief — the renderer draws racks, cables and code for it even unprompted.
TECH_HARDWARE = ("laptop", "computer", "monitor", "screen", "keyboard", "server", "rack", "cable",
                 "data center", "data centre", "terminal", "code")
_TECH_HARDWARE = re.compile(
    r"\b(?:" + "|".join(re.escape(t).replace(r"\ ", r"\s+") for t in TECH_HARDWARE)
    + r")(?:s|es)?\b", re.IGNORECASE)


# Round 12 (#2241): toy figurines, blank placeholder cards and coloured pin flags read as AI
# artifacts to the blind critic. No symbolic or metaphorical prop: hands are empty or the people
# interact.
_SYMBOLIC_PROPS = re.compile(
    r"\b(?:figurines?|tokens?|cards?|flags?|chess(?:\s+pieces?|\s+board)?|blocks?|"
    r"sticky\s+notes?|post-its?|game\s+pieces?|pawns?|dominoe?s?|"
    # Round 13: a coffee mug was in nearly every scene — monotony, not a story.
    r"mugs?|cups?|coffee|teas?|lattes?)\b", re.IGNORECASE)


def tech_hardware(text: Optional[str]) -> Optional[str]:
    """The first ``TECH_HARDWARE`` word in ``text``, or None.

    Args:
        text: An anchor or idea.

    Returns:
        The word as written.
    """
    hit = _TECH_HARDWARE.search(text or "")
    return hit.group(0) if hit else None


def prop_failure(text: Optional[str]) -> Optional[str]:
    """Why ``text`` names a prop that renders legible marks, or None.

    Args:
        text: A render prompt (hook stripped) or a visual idea.

    Returns:
        The reason: a paper/document/chart/code prop, or a screen not seen from behind.
    """
    text = text or ""
    symbolic = _SYMBOLIC_PROPS.search(text)
    if symbolic:
        return (f"the scene names {symbolic.group(0)!r} — a symbolic prop or a mug reads as an "
                f"AI artifact or stock monotony; hands are empty or the people interact")
    paper = _PAPER_PROPS.search(text)
    if paper:
        return (f"the scene names {paper.group(0)!r} — paper, charts and code render legible "
                f"text; tell the story with gesture, posture and the room instead")
    screen = _SCREEN_WORDS.search(text)
    if screen:
        return (f"the scene names a {screen.group(0)} — screens are the stock 'person at laptop' "
                f"trope; show people interacting in a real environment instead")
    phone = _PHONE_WORDS.search(text)
    if phone and not _FACE_DOWN.search(text):
        return (f"the scene names a {phone.group(0)} — a phone appears only held face-down")
    return None


def paper_rule_failure(prompt: str, rule: Optional[str]) -> Optional[str]:
    """Why ``prompt`` breaks the prop rule, or None (no rule passed: not checked).

    Args:
        prompt: The render prompt, hook stripped.
        rule: ``PAPER_FORBID`` or None.

    Returns:
        The rejection reason.
    """
    return prop_failure(prompt) if rule else None


def words_on_surface(text: Optional[str]) -> Optional[str]:
    """The first phrase in ``text`` that puts words on a surface, or None.

    Args:
        text: A prompt or a visual idea.

    Returns:
        The matched phrase.
    """
    match = _WORDS_ON_SURFACE.search(text or "")
    return match.group(0).strip() if match else None


def legible_document(text: Optional[str]) -> Optional[str]:
    """The first phrase in ``text`` that asks for a document or screen with readable content.

    Args:
        text: A prompt or a visual idea.

    Returns:
        The matched phrase ("a printed billing dashboard", "a paper check"), or None.
    """
    match = _LEGIBLE_DOCUMENT.search(text or "")
    return match.group(0).strip() if match else None


def unblanked_document(text: Optional[str]) -> Optional[str]:
    """A paper or screen ``text`` names without ever saying how it stays unreadable, or None.

    Args:
        text: A render prompt.

    Returns:
        The first document noun when no blank / steep-angle / out-of-focus qualifier is present.
    """
    text = text or ""
    match = _DOCUMENT_NOUNS.search(text)
    if match and not _UNREADABLE_QUALIFIERS.search(text):
        return match.group(0)
    return None


# Sentences that are AUTHOR context, never render content (round 4: the fallback shipped "The
# author is in the Artificial Intelligence, E-commerce industry. Make the visual feel on-brand…"
# as the first words of a render prompt).
_AUTHOR_CONTEXT_SENTENCES = re.compile(
    r"(?:The author is\b|Make the visual feel\b|When a person appears in the image it IS the "
    r"author\b|Describe that person consistently\b)[^.\n]*\.?\s*", re.IGNORECASE)


def strip_author_context(prompt: str, context: str = "") -> str:
    """``prompt`` with every author-context sentence removed — profile text never renders.

    Args:
        prompt: An authored or fallback render prompt.
        context: The exact context block the author was given, removed verbatim first.

    Returns:
        The prompt, trimmed.
    """
    if context:
        prompt = prompt.replace(context.strip(), " ")
    return " ".join(_AUTHOR_CONTEXT_SENTENCES.sub(" ", prompt or "").split())

# Round 3 (#2241): people and concrete scenes lost the palette entirely. A cover brief must name
# one deliberate brand-color element; these are the color words it may name it by.
_COLOR_WORDS = frozenset({
    "gold", "golden", "charcoal", "off-white", "cream", "ivory", "navy", "teal", "amber",
    "ochre", "mustard", "black", "white", "grey", "gray", "green", "blue", "red", "orange",
    "yellow", "purple", "burgundy", "slate", "sand", "beige", "copper", "bronze", "silver",
    "coral", "olive", "forest", "emerald", "crimson", "indigo", "magenta", "pink", "brown"})
_DEFAULT_BRAND_COLORS = frozenset({"gold", "golden", "charcoal", "off-white"})
BRAND_ACCENT_DIRECTIVE = (
    "BRAND ACCENT: brand colors are ACCENTS, never the overall tone — include exactly one "
    "deliberate brand-color element, named by its color ({colors}): the hook's color, a wardrobe "
    "accent, or a wall or background accent. Charcoal belongs only behind the hook, as its "
    "backing panel; the scene itself stays bright. Every other wardrobe and object color stays "
    "within gold, charcoal, off-white and natural tones — no saturated red, blue or green "
    "objects.\n")
# Round 5 (#2241, post gauntlet): every post render came back dark and moody — the charcoal brand
# neutral dragged whole scenes to near-black, which reads as murky in a white feed. A low-key
# scene is refused on the cover-recipe surfaces unless the emotional beat is itself dark.
_DARK_SCENE = re.compile(
    r"\b(?:moody|low[\s\-]key|dimly[\s\-]lit|dimly|dark\s+(?:room|office|scene|setting|"
    r"background|interior|studio)|shadowy|noir|chiaroscuro|pitch[\s\-]black|near[\s\-]black|"
    r"gloomy|murky|unlit)\b", re.IGNORECASE)
# Round 7: "relief" rendered as eyes squeezed shut and a hand on the chest — good news as pain.
_PAINED_RELIEF = re.compile(
    r"\beyes\s+(?:closed|shut|squeezed)\b|\b(?:closed|squeezed|shut)\s+eyes\b|"
    r"\bhand\s+(?:on|over|to)\s+(?:her|his|their|the)\s+(?:chest|heart)\b|"
    # Round 9: "Right model, right time" rendered a man holding his head, eyes shut — a headache.
    r"\bhands?\s+(?:on|over|to|against|pressed\s+to|cradling|rubbing|covering)\s+"
    r"(?:her|his|their|the)\s+(?:head|face|temples?|forehead|brow|eyes)\b|"
    r"\b(?:holding|clutching|cradling|rubbing|massaging)\s+(?:her|his|their)\s+"
    r"(?:head|face|temples?|forehead|brow)\b|"
    r"\b(?:head|face)\s+in\s+(?:her|his|their)\s+hands\b",
    re.IGNORECASE)
# Round 12: the critic called furrowed brows and an alarmed open mouth overacting.
_OVERACTED = re.compile(
    r"\bopen[\s\-]mouth(?:ed)?\b|\bmouths?\s+(?:wide\s+)?(?:open|agape|ajar)\b|\bgasp\w*\b|"
    r"\balarm(?:ed)?\b|\bfurrow\w*\s+brows?\b|\bbrows?\s+furrow\w*\b|\bfurrowed\b|"
    r"\bwide[\s\-]eyed\b|\bjaw[\s\-]drop\w*\b|\bshock(?:ed)?\b",
    re.IGNORECASE)
# Round 12: primary-colour pin flags clashed with the gold/charcoal/off-white brand. A saturated
# red, blue or green OBJECT is refused; a muted tone ("soft blue sweater") and nature ("green
# leaves", "blue hour") are not.
_SATURATED = re.compile(
    r"\b(?:red|blue|green|primary[\s\-]colou?red|neon|rainbow|multi[\s\-]?colou?red)\b"
    r"(?![\s\-](?:hour|plants?|leaves|foliage|trees?|eyes|eyed|sky))", re.IGNORECASE)
_MUTED = frozenset({"soft", "pale", "muted", "dusty", "faded", "light", "slate", "powder",
                    "sage", "olive", "navy", "washed", "deep", "dark", "greyish", "grayish"})


def saturated_color(text: Optional[str]) -> Optional[str]:
    """The first saturated red, blue or green (or primary-colour) word in ``text``, or None.

    Args:
        text: A render prompt.

    Returns:
        The colour word as written.
    """
    for match in _SATURATED.finditer(text or ""):
        before = re.findall(r"[a-z]+", (text or "")[:match.start()].lower())
        if before and before[-1] in _MUTED:
            continue
        return match.group(0)
    return None


_DARK_BEAT = re.compile(r"\b(?:dread|crisis|fear|grief|panic|despair|dark|ominous|loss)\b",
                        re.IGNORECASE)
# The surfaces that follow the cover recipe: a hook, a face with a readable emotion, no paper,
# blank screens, bright light (round 5 extended it from covers to post images).
HOOK_SURFACES = frozenset({"newsletter", "post_image"})
# Round 6: video frames brought paper back ("MONTHLY BILL") and scored brand_fit 1 with no hook,
# so the no-paper rule and the brand-accent gate cover them too.
PAPER_SURFACES = frozenset({"newsletter", "post_image", "video"})
BRAND_GATE_SURFACES = frozenset({"newsletter", "post_image", "video"})
# Round 6 addendum (#2241): the headline is COMPOSITED by ``image_compose``, never rendered. The
# brief only keeps the region the headline will sit on calm, so the typesetting has room.
NEGATIVE_SPACE: dict[str, str] = {
    "panel_left": ("keep the LEFT half of the frame calm, plain negative space — a soft wall or "
                   "background — with every subject in the right half; a headline panel is "
                   "added there later"),
    "panel_right": ("keep the RIGHT half of the frame calm, plain negative space — a soft wall or "
                    "background — with every subject in the left half; a headline panel is added "
                    "there later"),
    "full_bleed": ("keep one side third of the frame calm and slightly darker — a shadowed wall "
                   "or the window side — with nothing important in it; a headline is set over "
                   "it later"),
    "lower_third_band": ("keep every subject — faces, torsos and hands — in the UPPER 55% of the "
                         "frame, the bottom 45% calm and uncluttered floor or wall; a headline "
                         "band is added there later"),
    "band_top": ("keep the top 40% of the frame calm, plain negative space — wall, sky or "
                 "ceiling — with every subject in the lower 60%; a headline band is added there "
                 "later"),
}
# Round 8: the split layouts give the scene its own region, so the scene leaves no space at
# all — it is a square with the subject centred, filling it (it is centre-cropped beside the type).
# Round 10: no mention of a headline — a render told about one designs a cover around it.
_SQUARE_SCENE = "compose a SQUARE frame with the subject centred and filling it"
NEGATIVE_SPACE.update({layout: _SQUARE_SCENE for layout in
                       ("split_left", "split_right", "split_top", "split_bottom")})
_DEFAULT_NEGATIVE_SPACE = {"newsletter": "split_left", "post_image": "split_top"}


# Round 10 (#2241): told it was making a "LinkedIn newsletter cover" with "negative space" for a
# headline, gpt-image DESIGNED a cover — it typeset "ENGINEERING LEAD / Billing Dashboard" and
# "AI GOVERNANCE / LinkedIn Newsletter" into the empty side. Since round 8 the scene is its own
# square beside the type panel, so on a composited surface the RENDER prompt describes only a
# photograph; the use case stays author context and never reaches the renderer.
PHOTO_OPENING = "A candid documentary photograph"
PHOTO_FRAMING = "The subject is centred and fills the square frame."
_RENDER_FRAMING = re.compile(
    r"\blinked\s*in\b|\bnewsletters?\b|\bcovers?\b|\bheadlines?\b|\btitles?\b|"
    r"\btext\s+(?:area|space|panel|block|zone)s?\b|\bnegative\s+space\b|\bmagazines?\b|"
    r"\beditorial\s+layouts?\b|\btypeset\w*\b|\btypography\b|"
    r"(?<!lamp )(?<!goal )\bposts?\b(?!-it|\s+office)",
    re.IGNORECASE)
_PHOTO_LEAD = re.compile(
    r"^\s*an?\s+[^.:;]{0,80}?\bphotograph\b(?:\s+for\s+an?\s+[^.:;]*?(?=\s+of\b|[:,.;]))?",
    re.IGNORECASE)
_USE_CLAUSE = re.compile(
    r"\s+for\s+an?\s+[^.:;,]*?\b(?:cover|post|newsletter|linkedin|feed|slide)\b",
    re.IGNORECASE)


def label_tokens(hook: Optional[str], kicker: Optional[str],
                 anchors: Sequence[str] = ()) -> list[str]:
    """The hook and kicker words a render prompt must not repeat — the renderer paints them.

    A word that is also part of a visual anchor is the scene's SUBJECT, which the renderer draws
    rather than writes, so it stays allowed: forbidding it would make the anchor unpaintable.

    Args:
        hook: The headline ``image_compose`` typesets, or None.
        kicker: The kicker it typesets, or None.
        anchors: The concept's usable visual anchors.

    Returns:
        Lowercase words of 4+ letters, sorted.
    """
    anchor_text = " ".join(anchors).lower()
    words = set(re.findall(r"[a-z]+", f"{hook or ''} {kicker or ''}".lower()))
    return sorted(w for w in words
                  if len(w) >= 4 and w not in _FRAMING_STOPWORDS and w not in _FRAMING_FREE_WORDS
                  and not re.search(rf"\b{re.escape(w)}", anchor_text))


def _label_pattern(tokens: Sequence[str]) -> Optional["re.Pattern[str]"]:
    if not tokens:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(t) for t in tokens) + r")(?:s|es)?\b",
                      re.IGNORECASE)


def render_framing_failure(prompt: str, hook: Optional[str], kicker: Optional[str] = None,
                           anchors: Sequence[str] = ()) -> Optional[str]:
    """Why a composited surface's render prompt describes more than a photograph, or None.

    Args:
        prompt: The render prompt.
        hook: The headline typeset beside the render.
        kicker: The kicker typeset above it.
        anchors: The usable visual anchors (their words stay allowed).

    Returns:
        The rejection reason handed back to the author.
    """
    framing = _RENDER_FRAMING.search(prompt or "")
    if framing:
        return (f"the render prompt says {framing.group(0)!r} — the render is ONLY a photograph "
                f"(the headline is typeset beside it later); describe a candid documentary "
                f"photograph and nothing about where it is used")
    pattern = _label_pattern(label_tokens(hook, kicker, anchors))
    label = pattern.search(prompt or "") if pattern else None
    if label:
        return (f"the render prompt repeats the headline word {label.group(0)!r} — the renderer "
                f"paints it as text; show the situation without naming it")
    return None


def render_opening(concept: Optional[ImageConcept], treatment: Optional[str] = None) -> str:
    """How a composited render prompt opens: a documentary photograph, or the editorial style.

    Args:
        concept: Stage 1's concept.
        treatment: The brief's own treatment when it differs from the concept's (a fallback
            with no idea left renders a plain object photograph).

    Returns:
        ``PHOTO_OPENING``; for an ``editorial_concept`` its rotated ``ART_STYLES`` text,
        capitalised — the render is still ONE image with no use case, just not a photograph.
    """
    if concept is None or (treatment or concept.treatment) != TREATMENT_EDITORIAL:
        return PHOTO_OPENING
    style = ART_STYLES.get(concept.art_style or "", ART_STYLES[_DEFAULT_ART_STYLE])
    return style[:1].upper() + style[1:]


def photo_only_prompt(prompt: str, hook: Optional[str], kicker: Optional[str] = None,
                      anchors: Sequence[str] = (), opening: str = PHOTO_OPENING) -> str:
    """``prompt`` repaired to describe only a photograph: the deterministic backstop.

    The opening becomes ``opening`` (``PHOTO_OPENING`` unless an editorial concept passes its
    art style — and a prompt already opening with that style keeps it); a "for a LinkedIn …
    cover" use clause goes; any later sentence that frames a cover, a headline or text space is
    dropped whole; stray framing words and hook/kicker words are cut; and the square-frame
    instruction is appended when missing.

    Args:
        prompt: The render prompt.
        hook: The headline typeset beside the render.
        kicker: The kicker typeset above it.
        anchors: The usable visual anchors (their words stay).
        opening: The render's opening (``render_opening``).

    Returns:
        The repaired prompt; ``render_framing_failure`` passes it.
    """
    text = (prompt or "").strip()
    if opening != PHOTO_OPENING and text.lower().startswith(opening[:24].lower()):
        opened = 1
    else:
        text, opened = _PHOTO_LEAD.subn(opening, text, count=1)
    if not opened:
        text = f"{opening}: {text[:1].lower()}{text[1:]}"
    text = _USE_CLAUSE.sub("", text)
    sentences = re.split(r"(?<=[.;!?])\s+", text)
    text = " ".join([sentences[0]] + [s for s in sentences[1:] if not _RENDER_FRAMING.search(s)])
    text = _RENDER_FRAMING.sub("", text)
    pattern = _label_pattern(label_tokens(hook, kicker, anchors))
    if pattern:
        text = pattern.sub("", text)
    if "centred" not in text.lower() and "centered" not in text.lower():
        body = text.rstrip().rstrip(",;:")
        text = f"{body}{'' if body.endswith(('.', '!', '?')) else '.'} {PHOTO_FRAMING}"
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", text)
    text = re.sub(r"\b(an?|the)\s+(?=[,.;:]|(?:an?|the)\b)", "", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def _photo_only(brief: "ImageBrief", hook: Optional[str], kicker: str,
                anchors: Sequence[str]) -> "ImageBrief":
    """Repair a composited surface's brief in place when its prompt still frames a cover."""
    if hook and render_framing_failure(brief.prompt, hook, kicker, anchors):
        brief.prompt = photo_only_prompt(brief.prompt, hook, kicker, anchors,
                                         render_opening(brief.concept, brief.treatment))
    return brief


def negative_space_directive(layout: str, surface: str) -> str:
    """Which region of the frame the scene keeps calm for the composited headline.

    Args:
        layout: The concept's rotated compositing template ('' for the surface default).
        surface: ``newsletter`` or ``post_image``; other surfaces get no headline.

    Returns:
        The directive sentence, or '' when the surface carries no headline.
    """
    if surface not in _DEFAULT_NEGATIVE_SPACE:
        return ""
    return NEGATIVE_SPACE.get(layout) or NEGATIVE_SPACE[_DEFAULT_NEGATIVE_SPACE[surface]]


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
    """Does this image get the concept's hook composited on? Covers and post images only.

    ``treatment`` is accepted for the call sites' symmetry; since round 6 no render carries text,
    so a graphic on another surface gets no headline either.
    """
    _ = treatment
    return surface in HOOK_SURFACES


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
_BRIEF_MAX_TOKENS = 6000
# Stage 3's judge is lem-simple, also a reasoning model.
_CHECK_MAX_TOKENS = 6000

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
    return [a for a in anchors if not cliche_hit(a) and not name_tokens(a)
            and not prop_failure(a) and not tech_hardware(a)]


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
                           colors: Optional[frozenset[str]] = None,
                           paper_rule: Optional[str] = None,
                           bright: bool = False, positive: bool = False,
                           no_people: bool = False) -> Optional[str]:
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
    person = _PERSON_WORDS.search(unhooked) if no_people else None
    if person:
        return (f"an editorial concept shows no person ({person.group(0)!r}) — carry the idea "
                f"with the objects alone")
    named = _named_fact(unhooked, names or set())
    if named:
        return (f"the prompt names {named!r}, a fact the image cannot draw — show the visual "
                f"anchors, never a name or a number")
    overacted = _OVERACTED.search(unhooked)
    if overacted:
        return (f"the prompt overacts the face ({overacted.group(0)!r}) — restrained: concern "
                f"shows in posture and eyes with the mouth closed; good news is a quiet, "
                f"satisfied look")
    color = saturated_color(unhooked)
    if color:
        return (f"the prompt names a saturated {color!r} — wardrobe and objects stay within "
                f"gold, charcoal, off-white and natural tones")
    if positive and _RELIEF.search(unhooked) and "smile" not in unhooked.lower():
        return (f"\"relief\" alone reads as distress — for good news write: {POSITIVE_FACE}")
    pained = _PAINED_RELIEF.search(unhooked) if positive else None
    if pained:
        return (f"good news reads as pain ({pained.group(0)!r}) — {POSITIVE_FACE}, eyes open, "
                f"engaged with the other person or the task, hands away from the head and face")
    label = _GROUP_LABEL.search(unhooked)
    if label:
        return (f"the prompt names a group caption ({label.group(0)!r}) — the renderer paints "
                f"those onto posters and badges; describe people by appearance and action")
    dark = _DARK_SCENE.search(unhooked) if bright else None
    if dark:
        return (f"the prompt asks for a dark scene ({dark.group(0)!r}) — light it bright, high-key "
                f"or warm daylight with strong subject contrast; charcoal only behind the hook")
    words = words_on_surface(unhooked)
    if words:
        return (f"the prompt puts words on a surface ({words!r}) — screens and documents stay "
                f"blank or abstract")
    legible = legible_document(unhooked)
    if legible:
        return (f"the prompt asks for a readable document ({legible!r}) — drop it; tell the story "
                f"with gesture, posture and the room")
    # (A document "described as blank" is no longer enough — round 7 refuses the prop itself,
    # below — so the old unblanked-document check has nothing left to add.)
    # After the specific phrase checks, so their more precise reason is the one the author hears.
    paper = paper_rule_failure(unhooked, paper_rule)
    if paper:
        return paper
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
               colors: Optional[frozenset[str]] = None,
               paper_rule: Optional[str] = None, bright: bool = False,
               positive: bool = False, avatar: bool = False,
               no_people: bool = False) -> Optional[str]:
    """Why an authored brief is unusable, or None. The reason goes back to the author verbatim."""
    prompt = str(parsed.get("prompt") or "").strip()
    pose = _AVATAR_POSE.search(prompt) if avatar else None
    if pose:
        return (f"the author is posed ({pose.group(0)!r}) — show them mid-action with the "
                f"emotional beat in their expression and hands, gaze on the work, no mug or desk "
                f"props the anchors do not name")
    focal = str(parsed.get("focal_concept") or "").strip()
    if not (_MIN_PROMPT_CHARS <= len(prompt) <= _MAX_PROMPT_CHARS) or not (3 <= len(focal) <= 300):
        return "failed validation (length bounds)"
    lowered = prompt.lower()
    if any(fragment in lowered for fragment in _BANNED_FRAGMENTS):
        return "failed validation (refusal phrasing)"
    reply_hook = str(parsed.get("hook_text") or "").strip()
    if reply_hook.lower() not in ("", "null", "none") and not hook_text:
        return "the reply declared hook text, but the image carries no text — the headline is typeset later"
    return _deterministic_failure(prompt, anchors=anchors, weak=weak, hook_text=hook_text,
                                  names=names, colors=colors, paper_rule=paper_rule,
                                  bright=bright, positive=positive, no_people=no_people)


_CHECK_PROMPT = """You are checking an image prompt BEFORE it is sent to a renderer.

The piece's thesis: {thesis}
What the image should show: {anchors}
The image's headline: {hook}

The prompt:
<prompt>{prompt}</prompt>

Reading the headline together with an image rendered from this prompt, would a viewer get the
GIST of the claim? Not every detail or benefit — just the gist. Name which of the things above
the prompt depicts. Respond with ONLY a JSON object:
{{"guessable": true|false, "depicted_entities": ["..."], "reason": "<one short sentence>"}}"""


def _concept_is_weak(concept: Optional[ImageConcept], anchors: list[str]) -> bool:
    # An editorial concept is built on its chosen idea's OBJECTS; its anchors are often people
    # ("an agency owner"), which it must not draw — so anchor coverage stands down for it.
    return (concept is None or concept.weak or len(anchors) < WEAK_ENTITY_FLOOR
            or concept.treatment == TREATMENT_EDITORIAL)


@llm_step("image_prompt_check")
def _shows_nobody(concept: Optional[ImageConcept]) -> bool:
    return concept is not None and concept.treatment == TREATMENT_EDITORIAL


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
                                     hook_text=None, names=fact_name_tokens(concept),
                                     no_people=_shows_nobody(concept))
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
            reasoning_effort=REASONING_EFFORT,
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
    if treatment in (TREATMENT_PEOPLE, TREATMENT_CONCRETE, TREATMENT_EDITORIAL):
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
    # No prop anchor reaches a fallback (usable_anchors already drops them, round 7).
    anchors = [a for a in anchors if a]
    use = _USE_LABELS.get(surface, _USE_LABELS[_DEFAULT_PRESET])
    summary = _plain_words(concept.thesis if concept else content)[:240]
    if prop_failure(summary) or _GROUP_LABEL.search(summary):
        # Round 9: a thesis about "the pricing contract" rendered a sheet reading PRICING CONTRACT.
        summary = "the people this piece is about, working it through together"
    hook = (fit_hook(concept.hook_phrase, concept.thesis) if concept and concept.hook_phrase
            and carries_hook(surface, treatment) else None)
    accent = sorted(brand_colors(brand_kit) & {"gold", "golden"}) or sorted(brand_colors(brand_kit))
    finish = (f"composed for a {ratio} aspect ratio with the subject large in the frame, "
              f"bright, high-key warm daylight with strong subject contrast and one deliberate "
              f"{accent[0]} accent, subtle film "
              f"grain, real texture with fabric wear and everyday imperfections, plain unbranded "
              f"surfaces, clean unmarked walls, hands empty and relaxed.")
    space = negative_space_directive(concept.layout if concept else "", surface) if hook else ""
    layout = f" {space[:1].upper()}{space[1:]}." if space else ""
    # Profile context is AUTHOR context: a fallback is a render prompt, so it never carries it.
    context = ""
    idea = _plain_words(concept.chosen_idea) if concept and concept.chosen_idea else ""

    if treatment == TREATMENT_EDITORIAL:
        # #2241 showcase: with no surviving idea this used to become a CONCRETE scene of "the
        # everyday objects at the heart of this: not AI" — and the gate's emotion repair then
        # put a person in it. An editorial concept stays editorial and OBJECT-ONLY: its idea
        # when that names nobody, else a still life of the Idea Miner's own object nouns.
        style = ART_STYLES.get(getattr(concept, "art_style", "") or "", ART_STYLES[_DEFAULT_ART_STYLE])
        objects = editorial_objects(concept)
        if idea and not person_word(idea):
            subject = idea
        elif objects:
            subject = f"a still life of {_join(objects[:3])}, one of them oddly out of scale"
        else:
            subject = "a single everyday object from the reader's working world, oddly out of scale"
        prompt = (f"{context}{style[:1].upper()}{style[1:]}: {subject}. Objects only, one focal "
                  f"point centred on a flat calm ground with generous space around it, one "
                  f"deliberate {accent[0]} accent, composed for a {ratio} aspect ratio.{layout}")
    elif idea:
        who = (f", the person {cast_phrase(concept.cast)}"
               if concept and concept.cast and not avatar else "")
        if concept and concept.shot:
            who += f", framed as {concept.shot}"  # round 13: the rotated framing
        prompt = (f"{context}A photorealistic editorial photograph for a {use}: {idea}{who}, shot "
                  f"on a 50mm lens at f/2.8, visible skin pores, {finish}{layout}")
    elif treatment == TREATMENT_PEOPLE:
        who = _plain_words(concept.audience) if concept and concept.audience else ""
        if _GROUP_LABEL.search(who):
            who = ""  # "founders and content teams" is painted on a poster, never shown (round 9)
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
    prompt = strip_author_context(prompt)
    if hook:
        # Round 10: the composited render is only a photograph — no use case, no headline.
        prompt = photo_only_prompt(prompt, hook, concept_kicker(concept), usable_anchors(concept),
                                   render_opening(concept, treatment))
    return ImageBrief(prompt=prompt, ratio=ratio, surface=surface,
                      style_preset=surface if surface in _STYLE_PRESETS else _DEFAULT_PRESET,
                      focal_concept=(summary[:120] or "professional LinkedIn visual"),
                      fallback=True, concept=concept, treatment=treatment,
                      required_entities=tuple(a for a in anchors if entity_mentioned(a, prompt)),
                      hook_text=hook, rejections=tuple(rejections))


# Round 13: ed16 ("$30K saved") rendered alarmed four times — the model reads "relief" as
# distress. Positive valence is stated LITERALLY, and "relief" never stands alone.
POSITIVE_FACE = "a relaxed, genuine smile, eyes bright, shoulders loose"
_RELIEF = re.compile(r"\breliev\w*|\brelief\b", re.IGNORECASE)
_VALENCE_FACES = {
    "positive": (POSITIVE_FACE + ", engaged with the other person or the task — never eyes "
                 "closed, never a hand on the chest, head or face"),
    "negative": ("concern shown through posture and eyes, mouth closed — restrained, never an "
                 "open mouth, a furrowed brow or alarm"),
    "mixed": "wry or thoughtful",
}

# Post treatment rotation (#2241, anti-monotony round): an AI-rendered post scene rotates its photo
# GRADE least-recently-used (``post_treatment``). Every grade keeps the round-5 brightness rule —
# dusk is WARM, never dark — and every clause is phrased positively, because FLUX renders what a
# prompt names and naming "murky" or "dim" summons it. ``_DARK_SCENE`` must never match one.
GRADE_DAYLIGHT, GRADE_COOL_INTERIOR, GRADE_WARM_DUSK = "daylight", "cool_interior", "warm_dusk"
PHOTO_GRADES: dict[str, str] = {
    GRADE_DAYLIGHT: ("bright natural daylight, crisp neutral whites, true-to-life skin tones and "
                     "open, airy shadows"),
    GRADE_COOL_INTERIOR: ("a cool, clean interior grade: soft overcast window light, slate and "
                          "off-white tones, bright and even across every face"),
    GRADE_WARM_DUSK: ("a warm late-afternoon grade: low golden sunlight through a window, amber "
                      "highlights, every face fully and evenly lit and the whole frame bright and "
                      "readable with soft open shadows"),
}


def grade_clause(grade: Optional[str]) -> str:
    """The render-prompt sentence for a photo grade, or '' for an unknown or missing one.

    Args:
        grade: One of ``PHOTO_GRADES``.

    Returns:
        "Photo grade: …." — appended to an AI-rendered post scene's prompt.
    """
    text = PHOTO_GRADES.get(grade or "")
    return f"Photo grade: {text}." if text else ""


def with_grade(prompt: str, grade: Optional[str]) -> str:
    """``prompt`` with the grade's clause appended once; unchanged with no grade.

    Args:
        prompt: The render prompt.
        grade: One of ``PHOTO_GRADES``.

    Returns:
        The prompt, graded.
    """
    clause = grade_clause(grade)
    if not clause or clause in (prompt or ""):
        return prompt
    body = (prompt or "").rstrip()
    return f"{body}{'' if not body or body.endswith(('.', '!', '?')) else '.'} {clause}".strip()


def _analysis_block(concept: Optional[ImageConcept], anchors: list[str],
                    hook: Optional[str], surface: str = "post_image",
                    brand_kit: Optional[str] = None, avatar: bool = False) -> str:
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
    if concept.cast and not avatar:
        # Round 6: the series read as one man in his 30s-40s. The cast rotates; the role comes
        # from the piece's own anchors.
        block += (f"CAST: the person in frame is {cast_phrase(concept.cast)} — a real, "
                  f"unremarkable member of this audience, no stereotyped styling.\n")
    if concept.shot:
        # Round 13: every scene was a medium shot of 2-4 people at a table. Framing rotates.
        block += f"SHOT (framing — use it): {concept.shot}\n"
    if concept.setting:
        # Round 12: the place is the ARTICLE's — never a stock backdrop like a warehouse.
        block += f"SETTING (from the article — use it): {concept.setting}\n"
    if concept.valence:
        block += f"VALENCE: {concept.valence} — {_VALENCE_FACES.get(concept.valence, '')}.\n"
    if hook:
        block += (f"HEADLINE (typeset onto the image later by the system — never draw it, never "
                  f"quote it): {hook}\n")
        space = negative_space_directive(concept.layout, surface)
        if space:
            block += f"NEGATIVE SPACE: {space}.\n"
    return block


def video_accent_backstop(prompt: str, brand_kit: Optional[str] = None) -> str:
    """A video frame prompt that names a gold or charcoal accent — appended when it names none.

    #2241 showcase C: a frame failed brand_fit at 2 for "no gold accent". The author is asked for
    one (``VIDEO_FRAME_DIRECTIVE``) and an authored brief without one is refused, but a judged or
    fallback brief could still reach Runway without it. This is the last word, on every path —
    the avatar frame and the base frame are both authored here.

    Args:
        prompt: The final frame prompt.
        brand_kit: The brand clause, for the accent colour words.

    Returns:
        ``prompt``, or ``prompt`` plus one positive accent sentence.
    """
    colors = video_accent_colors(brand_colors(brand_kit))
    if _names_a_color(prompt or "", colors):
        return prompt
    color = "gold" if "gold" in colors else sorted(colors)[0]
    body = (prompt or "").rstrip()
    sep = "" if not body or body.endswith((".", "!", "?")) else "."
    return f"{body}{sep} One deliberate {color} accent object or wardrobe piece in frame.".strip()


# Showcase round 8: cover_17's backdrop came back a saturated blue — the palette directive steers
# objects and accents, and nothing named the GROUND. A render prompt on a composited surface that
# names no neutral backdrop gets one, stated positively (a render draws what a prompt names).
_NEUTRAL_WORDS = (r"off-white|cream|ivory|charcoal|grey|gray|beige|sand|stone|linen|warm\s+neutral|"
                  r"neutral|white|black|paper")
_BACKDROP_NAMED = re.compile(
    r"\b(?:background|backdrop|wall|walls|ground|sky)\b[^.]{0,40}\b(?:" + _NEUTRAL_WORDS + r")\b|"
    r"\b(?:" + _NEUTRAL_WORDS + r")\b[^.]{0,25}\b(?:background|backdrop|wall|walls)\b",
    re.IGNORECASE)
BRAND_BACKDROP_SENTENCE = "The backdrop is a warm off-white neutral, with gold kept to small accents."
BACKDROP_SURFACES = frozenset({"newsletter", "post_image"})


def palette_backdrop_backstop(prompt: str) -> str:
    """``prompt`` plus ``BRAND_BACKDROP_SENTENCE`` when it names no neutral backdrop.

    Args:
        prompt: The final render prompt.

    Returns:
        The prompt, or the prompt with one positive backdrop sentence.
    """
    if not (prompt or "").strip() or _BACKDROP_NAMED.search(prompt):
        return prompt
    body = prompt.rstrip()
    sep = "" if body.endswith((".", "!", "?")) else "."
    return f"{body}{sep} {BRAND_BACKDROP_SENTENCE}"


def build_image_brief(content: str, *, surface: str, ratio: str = "1:1",
                      profile=None, avatar: Optional[dict[str, Any]] = None,
                      extra_direction: Optional[str] = None,
                      content_shape: Optional[str] = None,
                      avoid_terms: Optional[list[str]] = None,
                      concept: "Optional[ImageConcept] | _NotAnalyzed" = NOT_ANALYZED,
                      brand_kit: Optional[str] = None) -> ImageBrief:
    """Author the brief for one render (``_author_image_brief``); a video frame's keeps its accent.

    Args:
        content: As for ``_author_image_brief``.
        surface: As for ``_author_image_brief``.
        ratio: As for ``_author_image_brief``.
        profile: As for ``_author_image_brief``.
        avatar: As for ``_author_image_brief``.
        extra_direction: As for ``_author_image_brief``.
        content_shape: As for ``_author_image_brief``.
        avoid_terms: As for ``_author_image_brief``.
        concept: As for ``_author_image_brief``.
        brand_kit: As for ``_author_image_brief``.

    Returns:
        The brief; on ``video`` its prompt always names a gold or charcoal accent
        (``video_accent_backstop``).
    """
    brief = _author_image_brief(content, surface=surface, ratio=ratio, profile=profile,
                                avatar=avatar, extra_direction=extra_direction,
                                content_shape=content_shape, avoid_terms=avoid_terms,
                                concept=concept, brand_kit=brand_kit)
    if surface == "video":
        brief.prompt = video_accent_backstop(brief.prompt, brand_kit)
    elif surface in BACKDROP_SURFACES:
        brief.prompt = palette_backdrop_backstop(brief.prompt)
    return brief


def _author_image_brief(content: str, *, surface: str, ratio: str = "1:1",
                        profile=None, avatar: Optional[dict[str, Any]] = None,
                        extra_direction: Optional[str] = None,
                        content_shape: Optional[str] = None,
                        avoid_terms: Optional[list[str]] = None,
                        concept: "Optional[ImageConcept] | _NotAnalyzed" = NOT_ANALYZED,
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
        concept: Stage 1's analysis when the caller already ran it — None included, which means
            it ran and came back empty, so it is NOT run again. Left at ``NOT_ANALYZED`` this runs
            Stage 1 itself.
        brand_kit: A pre-rendered brand clause (palette, type), folded into the AUTHOR's context
            only — never pasted into the fallback, which is a render prompt.

    Returns:
        The brief. ``fallback`` is True when the deterministic template shipped; ``rejections``
        carries why every rejected attempt was thrown out.
    """
    from cqc_lem.utilities.ai.ai_helper import _call_llm, _loads_json_object, _profile_visual_context
    from cqc_lem.utilities.avatar.attributes import subject_directive

    if isinstance(concept, _NotAnalyzed):
        try:
            concept = analyze_content_for_image(content, surface=surface)
        except Exception as e:  # analyze never raises, but the brief must not depend on that
            log_debug("Image concept stage raised — briefing without it", error=str(e),
                      surface=surface, action_type="image_brief")
            concept = None
        if concept is not None and not concept.layout and not concept.cast:
            # A concept this call computed gets a stable per-piece rotation (covers pass their
            # receipt history to Stage 1). A CALLER's concept is used exactly as given.
            concept = assign_layout_and_cast(concept, surface)

    preset = surface if surface in _STYLE_PRESETS else _DEFAULT_PRESET
    treatment = _treatment_for(concept, avatar)
    anchors = usable_anchors(concept)
    weak = _concept_is_weak(concept, anchors)
    no_people = _shows_nobody(concept) and not avatar
    names = fact_name_tokens(concept)
    hook = (fit_hook(concept.hook_phrase, concept.thesis) if concept and concept.hook_phrase
            and carries_hook(surface, treatment) else None)
    if hook:
        # Round 8: a composited surface renders a SQUARE scene that image_compose centre-crops
        # beside its type panel, whatever the caller's final ratio.
        ratio = "1:1"
    kicker = concept_kicker(concept) if hook else ""
    colors = brand_colors(brand_kit)
    # Deterministically enforced on covers; requested on every surface.
    gate_colors = colors if surface in BRAND_GATE_SURFACES else None
    if surface == "video":
        # A frame's accent must be a brand ACCENT — gold or charcoal — never the off-white
        # neutral that any wall already satisfies (#2241 showcase).
        gate_colors = video_accent_colors(colors)
    paper_rule = PAPER_FORBID  # every surface since round 7
    positive = concept is not None and concept.valence == "positive"
    bright = surface in HOOK_SURFACES and not (
        concept is not None and _DARK_BEAT.search(concept.emotional_beat or ""))
    # The likeness directive leads the context on purpose (issue #744): with nothing stating who
    # a depicted person is, the model invents one and the LoRA renders the invention.
    context = _profile_visual_context(profile, subject_directive(avatar))
    avoid = [t.strip() for t in (avoid_terms or []) if t and t.strip()][:_MAX_AVOID_TERMS]

    user_prompt = (
        f"{context}USE CASE: {_STYLE_PRESETS[preset]}\n"
        f"{treatment_text(treatment, concept)}\n\n"
        f"Compose for a {ratio} aspect ratio.\n"
        + _analysis_block(concept, anchors, hook, surface, brand_kit, avatar=bool(avatar))
        + (f"Brand: {brand_kit}\n" if brand_kit else "")
        + BRAND_ACCENT_DIRECTIVE.format(colors=", ".join(sorted(colors)))
        + (EDITORIAL_PROPS_DIRECTIVE if no_people else PROPS_DIRECTIVE)
        + (VIDEO_FRAME_DIRECTIVE if surface == "video" else "")
        + (avatar_directive(concept) if avatar else "" if no_people else _NO_AVATAR_PEOPLE)
        + ("" if avatar else gaze_directive(concept))
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
    attempts = _BRIEF_ATTEMPTS
    length_retry_used = False
    attempt = 0
    while attempt < attempts:
        attempt += 1
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
                reasoning_effort=REASONING_EFFORT,
            )
            choice = response.choices[0]
            raw = choice.message.content or ""
            if not raw.strip():
                # The shape an exhausted token budget takes: finish_reason='length', no content.
                finish_reason = getattr(choice, "finish_reason", None)
                reason = f"empty response (finish_reason={finish_reason})"
                rejections.append(reason)
                outages += 1
                if finish_reason == "length" and not length_retry_used:
                    # The budget ended the reply, not the model: ONE extra attempt (round 4).
                    length_retry_used, attempts = True, attempts + 1
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
            if isinstance(parsed.get("prompt"), str):
                parsed["prompt"] = strip_author_context(parsed["prompt"], context)
            rejection = _rejection(parsed, anchors=anchors, weak=weak, hook_text=None,
                                   names=names, colors=gate_colors, paper_rule=paper_rule,
                                   bright=bright, positive=positive, avatar=bool(avatar),
                                   no_people=no_people)
            if not rejection and surface == "video":
                rejection = video_frame_failure(str(parsed.get("prompt") or ""))
            if not rejection and hook:
                rejection = render_framing_failure(str(parsed.get("prompt") or ""), hook,
                                                   kicker, anchors)
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
                return _photo_only(brief, hook, kicker, anchors)
            ok, why = check_prompt_against_concept(prompt, concept, hook)
            brief.prompt_check = why
            if ok or attempt == attempts:
                return _photo_only(brief, hook, kicker, anchors)
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
        return _photo_only(judged_brief, hook, kicker, anchors)
    # WHY it fell back, every attempt's reason, at INFO: a validation fallback is the engine
    # working, not a fault. Only an author that never answered usably at all — every attempt an
    # exception, an empty reply or unparsable JSON — is an outage, and that alone is a warning.
    log_info("Image brief fell back to the deterministic template", surface=surface,
             action_type="image_brief", reason=reason, rejections=" | ".join(rejections))
    if outages == attempt:
        log_warning("Image brief author unavailable — deterministic template shipped",
                    surface=surface, action_type="image_brief", reason=reason)
    return _fallback_brief(content, surface=surface, ratio=ratio, context=context, avatar=avatar,
                           concept=concept, treatment=treatment, rejections=tuple(rejections),
                           brand_kit=brand_kit)
