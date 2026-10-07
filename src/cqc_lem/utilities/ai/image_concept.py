"""Stage 1 of the image engine: read the WHOLE piece and decide what its image should show.

Before this stage the brief author saw a title, a few candidate objects and one mechanism sentence,
so every newsletter cover became the same metaphor still-life — a valve, a gauge, a gear — whatever
the article was about (issue #2241). This module reads up to ``_MAX_SOURCE_CHARS`` of the actual
text and returns an ``ImageConcept``: the thesis, who it is for, the FACTS the text names, the
DEPICTABLE things it is about, and the TREATMENT the image should take. ``image_brief`` writes the
render prompt from that, and ``image_gen``'s vision gate grades the render against it.

Two lists, on purpose (gauntlet round 1 of #2241). ``specific_entities`` are facts — company and
product names, report titles, numbers — and they feed the thesis and hook, but a renderer cannot
DRAW "GPT-5.2" or "Stanford HAI": it either writes the name as garbled text or invents a prop that
"represents" it. ``visual_anchors`` are what the image actually shows — roles, places, physical
artifacts, situations — and they are refused deterministically when they carry a name or a number.

Grounding is enforced, not requested: an entity the text never mentions is dropped, because an
invented entity is how an article-specific brief turns back into a generic one.

Fails soft: any error returns ``None`` and every caller has a deterministic path without it.
"""

import dataclasses
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Optional, Sequence

from cqc_lem.utilities.logger import log_debug, log_info
from cqc_lem.utilities.observability import llm_step

TREATMENT_PEOPLE = "people_scene"
TREATMENT_GRAPHIC = "editorial_graphic"
TREATMENT_CONCRETE = "concrete_scene"
TREATMENT_METAPHOR = "metaphor_last_resort"
TREATMENTS = (TREATMENT_PEOPLE, TREATMENT_GRAPHIC, TREATMENT_CONCRETE, TREATMENT_METAPHOR)

# Enough of a long newsletter edition to reach its payoff, which usually sits well past the hook.
_MAX_SOURCE_CHARS = 12000
_MIN_ENTITIES, _MAX_ENTITIES = 3, 6
_MAX_ANCHORS = 5
# Below this many usable visual anchors the concept is WEAK: still usable for treatment and
# thesis, but the anchor-coverage checks downstream stand down rather than demand what is not
# there.
WEAK_ENTITY_FLOOR = 2
# Round 6: the headline is typeset by ``image_compose``, so it may run to six words.
_HOOK_MIN_WORDS, _HOOK_MAX_WORDS, _HOOK_MAX_CHARS = 2, 6, 44
# Round 6 (blind critic): every hook was "AI X: N% Y". The SHAPE rotates least-recently-used.
HOOK_SHAPES = ("number_claim", "contrast", "question", "plain_claim")
VALENCES = ("positive", "negative", "mixed")
# Words a hook may use that the source need not contain: function words, common verbs and
# adjectives. Everything else in a hook — every number and every other content word — must come
# from the piece itself (the critic caught "reach" for "engagement", "influencers" for "hidden
# buyers"). AI and LLM are always allowed.
_HOOK_FREE_WORDS = frozenset({
    "you", "your", "yours", "they", "their", "them", "who", "what", "why", "how", "when", "which",
    "still", "really", "just", "only", "even", "never", "always", "more", "less", "most", "least",
    "fewer", "than", "not", "isn", "aren", "don", "doesn", "can", "cannot", "will", "won",
    "would", "should", "could", "must", "need", "needs", "get", "gets", "got", "make", "makes",
    "made", "keep", "keeps", "find", "finds", "lose", "loses", "lost", "cost", "costs", "pay",
    "pays", "paid", "buy", "buys", "win", "wins", "miss", "misses", "missed", "work", "works",
    "worked", "fail", "fails", "failed", "beat", "beats", "see", "sees", "say", "says", "know",
    "knows", "think", "thinks", "want", "wants", "use", "uses", "used", "go", "goes", "come",
    "comes", "take", "takes", "give", "gives", "enough", "wrong", "right", "real", "really",
    "big", "small", "new", "old", "same", "every", "all", "some", "any", "one", "first", "last",
    "hidden", "silent", "quiet", "cheap", "cheaper", "cheapest", "better", "best", "worse",
    "worst", "true", "false", "good", "bad", "half", "twice", "times", "percent", "here", "there",
    "now", "yet", "already", "again", "away", "off", "out", "over", "under", "behind", "ahead",
    "on", "ai", "llm", "llms",
})
# lem-medium is a reasoning model: thinking tokens bill against this budget before any JSON. 2500
# still ran out on real editions (gauntlet round 3: finish_reason=length, empty content), so every
# JSON call in the engine now gets a budget a reasoning model finishes inside, asks for LOW
# reasoning effort (the proxy drops the param for a model that has none — `drop_params: true`),
# and retries ONCE on a length cut before giving up.
_CONCEPT_MAX_TOKENS = 6000
REASONING_EFFORT = "low"
# The thesis is ONE gist-level claim (round 3: a thesis listing three benefits made every judge
# say "it does not convey the compliance and lead-quality benefits").
THESIS_MAX_WORDS = 20
# A hook sharing this much of its vocabulary with the title is the title again.
_HOOK_TITLE_OVERLAP = 0.6
# Title Case is a headline style the brand does not use; three capitalised words is Title Case.
_TITLE_CASE_WORDS = 3
# At most ONE editorial_graphic in any GRAPHIC_WINDOW consecutive covers (gauntlet round 1: every
# edition chose a graphic, so the series had no variety at all).
GRAPHIC_WINDOW = 3

# Generic acronyms that are not anyone's name. Everything else that is capitalised mid-phrase, or
# carries a digit, is a name or a number — a FACT, never something to draw.
GENERIC_ACRONYMS = frozenset({"AI", "B2B", "B2C", "CEO", "CFO", "CTO", "COO", "CMO", "HR", "IT",
                              "SaaS"})

# Round 5 (#2241): "45% less engagement" says how much but not OF WHAT. A numeric hook must name
# its subject, so these words never count as one — they are what a number is ABOUT, not the thing.
_GENERIC_HOOK_WORDS = frozenset({
    "less", "more", "fewer", "lower", "higher", "engagement", "reach", "growth", "wasted", "waste",
    "lost", "loss", "lose", "cost", "costs", "spend", "spent", "saved", "savings", "drop", "drops",
    "increase", "decrease", "gain", "gains", "miss", "missed", "lies", "wrong", "right", "faster",
    "slower", "better", "worse", "percent", "times", "every", "only", "still", "just", "never",
    "always", "mark", "fall", "flat", "down", "off", "per", "cent", "half", "most", "least",
})

# A hook that shouts or orders the reader about is an ad, not a curiosity gap.
_IMPERATIVE_OPENERS = frozenset({
    "stop", "start", "don't", "dont", "do", "never", "always", "quit", "avoid", "try", "make",
    "get", "use", "learn", "discover", "unlock", "boost", "grow", "build", "fix", "cut", "ditch",
})

_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "into", "onto", "your", "their", "our",
    "its", "his", "her", "a", "an", "of", "on", "in", "at", "to", "by", "or", "as", "is", "are",
    "was", "were", "be", "who", "what", "how", "why", "when",
})

_SYSTEM_PROMPT = f"""You are the photo editor for a LinkedIn author. You read ONE piece of content \
and decide what its single image must show so a stranger scrolling past could guess what the \
piece argues.

Respond with ONLY a JSON object:
{{"thesis": "<the piece's ONE central claim, at most 20 words — never a list of benefits>",
 "audience": "<who it is written for, a few words>",
 "specific_entities": ["<3-6 facts the text names: companies, products, reports, numbers, roles>"],
 "visual_anchors": ["<3-5 DEPICTABLE things from the piece: roles, places, physical artifacts, \
actions or situations>"],
 "emotional_beat": "<the feeling the reader should get, a few words>",
 "valence": "positive|negative|mixed",
 "kicker": "<1-3 word UPPERCASE topic tag, words from the article: AI CONTENT AUDIT, LLM COSTS>",
 "hook_phrase": "<2-6 words>",
 "hook_alternatives": ["<two more hooks, same rules>"],
 "hook_candidates": {{"number_claim": "<…>", "contrast": "<…>", "question": "<…>",
                     "plain_claim": "<…>"}},
 "visual_ideas": ["<3 one-sentence image ideas>"],
 "treatment": "{'|'.join(TREATMENTS)}",
 "treatment_rationale": "<one sentence>"}}

Rules for specific_entities: copy them FROM THE TEXT. Never invent one. These are FACTS: they \
inform the thesis and the hook, and they are never drawn.

Rules for visual_anchors: things a camera could photograph, drawn from the piece — "a marketing \
lead", "a client kickoff meeting", "a stack of printed proposals", "a shared office kitchen". \
NEVER a brand, product, company or model name, a report title, or a number: a renderer cannot \
draw "GPT-5.2" or "a 2025 report", it writes the words as garbled text. Write them in lowercase.

Rules for hook_phrase and hook_alternatives: a curiosity gap a reader would want closed, or a \
concrete contrast drawn from the thesis. Never an exclamation, never an order to the reader \
("Stop…", "Start…", "Don't…"), never a restatement of the title. Sentence case, never Title \
Case, 2-6 words. Every number and every noun in a hook comes from the article's OWN words — \
"engagement" stays "engagement", never "reach"; "hidden buyers" never becomes "influencers". A \
numeric hook names its subject: "45% less engagement on AI posts", never "45% less \
engagement". Give one hook in EACH shape in hook_candidates — number_claim ("45% less engagement \
on AI posts"), contrast ("Search on, still wrong"), question ("Who really buys your AI?"), \
plain_claim ("Your cheapest model is enough") — the series rotates through them. The hook \
carries the THESIS; the image carries emotion and specificity.

Rules for valence: positive when the piece is about a saving, a win or relief; negative when it \
is about a risk, a loss or a mistake; mixed otherwise. The face in the image follows it.

Rules for kicker: the 1-3 word topic tag a magazine prints above a headline — "AI CONTENT AUDIT", \
"LLM COSTS", "AI FACT-CHECKING", "B2B BUYING". Every word comes from the article. It is what tells \
a scroller what the cover is ABOUT, so the scene never has to.

Rules for visual_ideas: exactly 3 one-sentence ideas, each combining the hook with a concrete \
scene or juxtaposition built from the visual anchors and the emotional beat — for an article \
about AI hallucination, "A copy editor's red pen frozen mid-strike over a confidently printed \
paragraph, her face a half-smile of disbelief." At least ONE idea is people-led: a visible face \
with a specific reaction. No names, no numbers, no stock symbols, and never a person merely \
looking at or typing on a laptop.

Choose the treatment:
- {TREATMENT_PEOPLE}: the piece is about people, teams, clients, hiring or a decision someone \
makes. The image is a candid documentary photograph of those people in that situation.
- {TREATMENT_GRAPHIC}: ONLY when a single number or a single contrast IS the thesis. The image is \
a designed editorial graphic carrying the hook_phrase.
- {TREATMENT_CONCRETE}: the piece describes a specific tangible situation — a pile of invoices, \
a cluttered workbench, a warehouse shelf.
- {TREATMENT_METAPHOR}: ONLY when nothing concrete exists in the text. Even then, an uncommon \
metaphor specific to this piece — never a stock symbol such as a lightbulb, gears, a puzzle \
piece, pipes or valves, a rocket, a chess board, a compass, money or a handshake."""

# Covers lean on people (gauntlet round 1 of #2241): LinkedIn's own creative guidance is that
# real faces outperform clipart and symbols, and a series of graphics is a series of ads.
_COVER_GUIDANCE = (
    "This is a newsletter COVER. Prefer people_scene — LinkedIn's guidance is that real faces "
    "beat clipart and symbols. Use editorial_graphic only when a single number or contrast IS "
    "the thesis. A cover ALWAYS carries its hook as a headline, whatever the treatment, so "
    "hook_phrase is required. A concrete_scene only when the idea is a striking, specific object "
    "juxtaposition — never an object sitting on a desk.")

# Round 5: the post image follows the cover recipe — a hook, a face, a bright scene.
_POST_GUIDANCE = (
    "This is a LinkedIn feed POST image (4:5). Prefer people_scene — a real face with a readable "
    "reaction stops a scroll; a concrete_scene only when the idea is a striking, specific object "
    "juxtaposition, never an object sitting on a desk. The image ALWAYS carries its hook as a "
    "headline, so hook_phrase is required.")
# An idea that is just an object resting on furniture is a still life, not a scroll-stopper.
_OBJECT_ON_FURNITURE = re.compile(
    r"\bon\s+(?:a|an|the|his|her|their)?\s*(?:[\w\-]+\s+){0,2}"
    r"(?:desk|table|shelf|counter|workbench|desktop)\b", re.IGNORECASE)

# Round 3 (#2241): abstract theses ("AI hallucinates", "hidden buyers") cannot be carried by a
# literal scene, so Stage 1 proposes several ideas and a cheap judge picks one (the Idea2Img
# pattern: generate candidates, rank, build from the winner).
_VISUAL_IDEAS = 3
_PEOPLE_WORDS = re.compile(
    r"\b(?:face|faces|her|his|their|she|he|person|people|man|woman|men|women|team|colleague|"
    r"founder|owner|lead|manager|editor|analyst|marketer|consultant|client|buyer|officer|"
    r"executive|expression|smile|frown|wince|laugh|eyebrow)s?\b", re.IGNORECASE)
_IDEA_PICK_MAX_TOKENS = 6000
_IDEA_PICK_PROMPT = """You are picking ONE image idea for a LinkedIn {surface}.

The piece argues: {thesis}
Its headline (hook): {hook}

Candidate ideas:
{ideas}

Rank them on: specificity to THIS article, surprise, legibility as a 400x225 thumbnail, and \
distance from stock clichés. Penalise an object sitting on a desk or table with no person — a \
face with a readable reaction stops a scroll; a still life does not. Respond with ONLY a JSON \
object:
{{"ranking": [<idea numbers, best first>], "reason": "<one sentence on the winner>"}}"""


@dataclass(frozen=True)
class ImageConcept:
    """What one piece of content is about, reduced to what its image must show.

    Attributes:
        thesis: The piece's actual argument in one sentence.
        audience: Who the piece is written for.
        specific_entities: FACTS the text names — grounded in the source, never drawn.
        emotional_beat: The feeling the image should carry.
        hook_phrase: A 2-5 word curiosity gap; only an ``editorial_graphic`` ever renders it.
        treatment: One of ``TREATMENTS``.
        treatment_rationale: Why that treatment, as the analyst put it (plus any deterministic
            override, appended).
        weak: True when fewer than ``WEAK_ENTITY_FLOOR`` visual anchors survived validation, so
            the anchor-coverage checks downstream stand down.
        visual_anchors: The DEPICTABLE things the image shows — no names, no numbers.
        visual_ideas: Up to three one-sentence image ideas that survived the deterministic filters
            (no stock symbol, no name or number).
        chosen_idea: The idea the cheap ranking call picked; Stage 2 builds from it.
        rejected_ideas: The ideas it did not pick, kept for the receipt.
        idea_pick_reason: Why it picked the winner, or why it could not rank.
        layout: The rotated compositing template for the headline (``assign_layout_and_cast``).
        cast: The rotated person hint for a people_scene, as a dict of its dimensions.
        valence: positive / negative / mixed — the emotion's direction must match it.
        hook_shape: Which ``HOOK_SHAPES`` the chosen hook takes.
        hook_options: Every valid hook Stage 1 offered, by shape, for the rotation to pick from.
        kicker: The 1-3 word UPPERCASE topic tag ``image_compose`` sets above the headline, every
            word grounded in the source ('' when none survived validation).
    """

    thesis: str
    audience: str
    specific_entities: tuple[str, ...]
    emotional_beat: str
    hook_phrase: str
    treatment: str
    treatment_rationale: str
    weak: bool = False
    visual_anchors: tuple[str, ...] = ()
    visual_ideas: tuple[str, ...] = ()
    chosen_idea: str = ""
    rejected_ideas: tuple[str, ...] = ()
    idea_pick_reason: str = ""
    layout: str = ""
    cast: Optional[dict[str, str]] = None
    valence: str = "mixed"
    hook_shape: str = ""
    hook_options: Optional[dict[str, str]] = None
    kicker: str = ""

    def to_dict(self) -> dict[str, Any]:
        """The concept as a JSON-safe dict, for prompts and receipts."""
        return asdict(self)


def _content_tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
            if t not in _STOPWORDS and (len(t) >= 3 or t.isdigit())]


def _stem(token: str) -> str:
    # Loose on purpose: "invoices" must match "invoice", "hiring" must match "hire".
    if token.isdigit() or len(token) <= 4:
        return token
    return token[:max(4, len(token) - 3)]


def entity_mentioned(entity: str, text: str) -> bool:
    """Is ``entity`` named in ``text``? Case-insensitive, by loose word stems.

    True when at least half of the entity's content words (rounded up) start a word in ``text``.
    Used both to ground Stage 1's entities in the source and to check that a brief actually
    depicts its anchors.

    Args:
        entity: A short noun phrase, e.g. ``"unpaid invoices"``.
        text: The text to search.

    Returns:
        Whether the entity is mentioned. An entity with no content words is never mentioned.
    """
    tokens = _content_tokens(entity)
    if not tokens:
        return False
    lowered = (text or "").lower()
    hits = sum(1 for t in tokens if re.search(rf"\b{re.escape(_stem(t))}", lowered))
    return hits >= (len(tokens) + 1) // 2


def name_tokens(entity: str) -> list[str]:
    """The tokens of ``entity`` that make it a NAME or a NUMBER — what a render must never carry.

    A token counts when it holds a digit (``2025``, ``GPT-5.2``, ``$30K``), carries an uppercase
    letter past its first character (``LinkedIn``, ``HAI``), or is capitalised anywhere but the
    first word. A capitalised FIRST word alone is ambiguous ("Payroll run" vs "Terralogic") and is
    resolved against the source at parse time (``parse_concept``). ``GENERIC_ACRONYMS`` never count.

    Args:
        entity: One entity or anchor phrase.

    Returns:
        The offending tokens, as written; empty for a plain common-noun phrase.
    """
    tokens = re.findall(r"[$€£]?[A-Za-z0-9][A-Za-z0-9.%'’\-]*", entity or "")
    found = []
    for index, token in enumerate(tokens):
        bare = token.strip(".-'’")
        if bare in GENERIC_ACRONYMS:
            continue
        if (any(ch.isdigit() for ch in bare) or any(ch.isupper() for ch in bare[1:])
                or (index > 0 and bare[:1].isupper())):
            found.append(bare)
    return found


def is_fact_only(entity: str) -> bool:
    """True when ``entity`` is a name or a number — a fact the image may never draw or write.

    Args:
        entity: One ``specific_entities`` item (as normalised by ``parse_concept``).

    Returns:
        Whether it carries a name token, or opens with a capitalised word (a proper noun once
        ``parse_concept`` has lowercased every capitalised common word it could ground).
    """
    entity = (entity or "").strip()
    return bool(name_tokens(entity)) or (entity[:1].isupper()
                                         and entity.split()[0] not in GENERIC_ACRONYMS)


def _clean(value: Any, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def names_its_subject(hook: str, topic_text: str) -> bool:
    """Does a hook name what it is about, beyond a number and generic words?

    True when it carries a generic acronym the piece is about ("AI"), or a content word that is
    not a generic hook word and that the piece's own title, thesis, facts or anchors use.

    Args:
        hook: The hook.
        topic_text: The concept's title, thesis, facts and anchors, joined.

    Returns:
        Whether the hook names its subject.
    """
    if any(w.strip(".,:;?'’") in GENERIC_ACRONYMS for w in hook.split()):
        return True
    topic = (topic_text or "").lower()
    return any(re.search(rf"\b{re.escape(_stem(t))}", topic)
               for t in _content_tokens(hook)
               if t not in _GENERIC_HOOK_WORDS and not any(ch.isdigit() for ch in t))


def _root(token: str) -> str:
    for suffix in ("ings", "ers", "ing", "ies", "ied", "ed", "es", "er", "ly", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: len(token) - len(suffix)]
    return token


def hook_is_faithful(hook: str, source: str) -> bool:
    """Is every number and every content word in ``hook`` from the piece's own words?

    Numbers must appear in the source verbatim (digits); every other content word must match a
    word in the source by its root ("buyers" ↔ "buyer"), unless it is a ``_HOOK_FREE_WORDS``
    function word, common verb or adjective. AI and LLM are always allowed.

    Args:
        hook: The hook.
        source: The analysed text (title included).

    Returns:
        Whether the hook drifts from nothing the piece says.
    """
    lowered = (source or "").lower()
    for number in re.findall(r"\d+(?:[.,]\d+)?", hook):
        if number not in lowered:
            return False
    for token in re.findall(r"[a-z][a-z'’]*", hook.lower()):
        token = token.split("'")[0].split("’")[0]
        if len(token) < 3 or token in _STOPWORDS or token in _HOOK_FREE_WORDS:
            continue
        if not re.search(rf"\b{re.escape(_root(token))}", lowered):
            return False
    return True


_KICKER_ACRONYMS = frozenset({"ai", "llm", "llms", "b2b", "b2c", "saas", "seo", "crm", "roi"})
_KICKER_MAX_WORDS = 3


def valid_kicker(kicker: Any, source: str) -> str:
    """The kicker, uppercased, if it is 1-3 words all drawn from the source; else ''.

    Every word must match a source word by its root (hyphenated parts each count: "FACT-CHECKING"
    needs "fact" and "check"); the generic acronyms AI, LLM, B2B… always pass.

    Args:
        kicker: Stage 1's raw kicker.
        source: The analysed text.

    Returns:
        The uppercase kicker, or ''.
    """
    text = " ".join(str(kicker or "").split()).strip(" .,:;")
    words = text.split()
    if not (1 <= len(words) <= _KICKER_MAX_WORDS) or len(text) > 32:
        return ""
    lowered = (source or "").lower()
    for part in re.findall(r"[a-z0-9]+", text.lower()):
        if part in _KICKER_ACRONYMS or part in _STOPWORDS:
            continue
        if not re.search(rf"\b{re.escape(_root(part))}", lowered):
            return ""
    return text.upper()


def hook_shape_of(hook: str) -> str:
    """Classify a hook into one of ``HOOK_SHAPES``.

    Args:
        hook: The hook.

    Returns:
        question (ends with ?), number_claim (has a digit), contrast (a comma, colon or "but"),
        else plain_claim.
    """
    if hook.rstrip().endswith("?"):
        return "question"
    if any(ch.isdigit() for ch in hook):
        return "number_claim"
    if re.search(r",|:|\bbut\b|\byet\b|\bstill\b", hook, re.IGNORECASE):
        return "contrast"
    return "plain_claim"


def _valid_hook(hook: str, title: Optional[str], topic_text: str = "",
                source: Optional[str] = None) -> str:
    """The hook if it is a usable curiosity gap; else ''.

    2-5 words, at most ``_HOOK_MAX_CHARS``, no exclamation mark, no imperative opener, not Title
    Case, not the title again — and a hook with a number in it must name its subject
    (``names_its_subject``), so "45% less engagement" is refused and "45% less reach for AI posts"
    is not.
    """
    hook = hook.strip().strip("\"'“”‘’").strip()
    words = hook.split()
    if not (_HOOK_MIN_WORDS <= len(words) <= _HOOK_MAX_WORDS) or len(hook) > _HOOK_MAX_CHARS:
        return ""
    if "!" in hook or words[0].lower().strip(",.:;") in _IMPERATIVE_OPENERS:
        return ""  # an order or a shout is an ad, not a curiosity gap
    if is_title_case(hook):
        return ""  # the brand sets headlines in sentence case
    hook_tokens = set(_content_tokens(hook))
    title_tokens = set(_content_tokens(title or ""))
    if hook_tokens and title_tokens and (
            len(hook_tokens & title_tokens) / len(hook_tokens) >= _HOOK_TITLE_OVERLAP):
        return ""  # a near-restatement of the title closes no gap
    if any(ch.isdigit() for ch in hook) and not names_its_subject(
            hook, f"{title or ''} {topic_text}"):
        return ""  # a number with no subject says how much, never of what
    if source is not None and not hook_is_faithful(hook, source):
        return ""  # a number or noun the piece never says
    # Sentence case: the first letter up (round 7: "audit stops AI waste"); acronyms untouched.
    return hook[:1].upper() + hook[1:] if hook[:1].islower() else hook


def is_title_case(text: str) -> bool:
    """True when ``text`` capitalises ``_TITLE_CASE_WORDS`` or more words (acronyms excepted).

    Args:
        text: A hook.

    Returns:
        Whether it reads as Title Case — "When AI Misses the Mark" does, "53.7% miss the mark"
        and "Web search isn't enough" do not.
    """
    capitalised = [w for w in (text or "").split()
                   if w[:1].isupper() and not (len(w) >= 2 and w.strip(".,:;?'’").isupper())]
    return len(capitalised) >= _TITLE_CASE_WORDS


def gist_thesis(thesis: str) -> str:
    """ONE claim of at most ``THESIS_MAX_WORDS`` words: the first clause, never a benefit list.

    Args:
        thesis: Stage 1's thesis as written.

    Returns:
        The text up to the first ``;``, cut to ``THESIS_MAX_WORDS`` words.
    """
    first = re.split(r";|—| - ", thesis or "", maxsplit=1)[0].strip().rstrip(",")
    words = first.split()
    return " ".join(words[:THESIS_MAX_WORDS]).rstrip(",;:") if words else ""


def _common_casing(entity: str, source: str) -> str:
    """Lowercase a capitalised first word the source also uses in lowercase — a common noun."""
    first, _, rest = entity.partition(" ")
    if (first[:1].isupper() and first[1:].islower()
            and re.search(rf"\b{re.escape(first.lower())}\b", source)):
        return f"{first.lower()} {rest}".strip()
    return entity


def _ground_entities(raw: Any, source: str) -> tuple[str, ...]:
    entities: list[str] = []
    for item in (raw if isinstance(raw, list) else []):
        entity = _clean(item, 80)
        if not entity or entity.lower() in (e.lower() for e in entities):
            continue
        if entity_mentioned(entity, source):
            entities.append(_common_casing(entity, source))
        else:
            log_debug("Image concept entity dropped — not in the source text", entity=entity,
                      action_type="image_concept")
    return tuple(entities[:_MAX_ENTITIES])


def anchor_rejection(anchor: str, facts: Sequence[str], source: Optional[str] = None) -> str:
    """Why ``anchor`` cannot be drawn, or '' when it can.

    Refused when it carries a digit, a capitalised token past its first word, a capitalised first
    word the source never uses in lowercase (a name), or any token of a proper-noun fact.

    Args:
        anchor: One candidate visual anchor.
        facts: The concept's ``specific_entities``.
        source: The analysed text, when available.

    Returns:
        A short reason, or ``''``.
    """
    names = name_tokens(anchor)
    if names:
        return f"names or numbers {names}"
    first = anchor.split()[0] if anchor.split() else ""
    if (source is not None and first[:1].isupper() and first not in GENERIC_ACRONYMS
            and not re.search(rf"\b{re.escape(first.lower())}\b", source)):
        return f"the capitalised {first!r} is a name"
    lowered = {t.lower() for t in re.findall(r"[A-Za-z0-9]+", anchor)}
    for fact in facts:
        if is_fact_only(fact):
            fact_names = {t.lower() for t in name_tokens(fact)} | (
                {fact.split()[0].lower()} if fact[:1].isupper() else set())
            if lowered & fact_names:
                return f"it names the fact {fact!r}"
    return ""


def _ground_anchors(raw: Any, source: str, facts: Sequence[str]) -> tuple[str, ...]:
    anchors: list[str] = []
    lowered_source = source.lower()
    for item in (raw if isinstance(raw, list) else []):
        anchor = _clean(item, 80)
        if not anchor or anchor.lower() in (a.lower() for a in anchors):
            continue
        reason = anchor_rejection(anchor, facts, source)
        grounded = any(re.search(rf"\b{re.escape(_stem(t))}", lowered_source)
                       for t in _content_tokens(anchor))
        if reason or not grounded:
            log_debug("Image concept anchor dropped", anchor=anchor, action_type="image_concept",
                      reason=reason or "nothing in it appears in the source")
            continue
        anchors.append(anchor[:1].lower() + anchor[1:] if anchor[:1].isupper() else anchor)
    return tuple(anchors[:_MAX_ANCHORS])


def idea_rejection(idea: str, facts: Sequence[str]) -> str:
    """Why a visual idea cannot be built, or '' when it can.

    The same deterministic filters every render prompt faces, run BEFORE the ranking call so it
    never picks an idea the brief would then refuse: a stock symbol, a name, or a number.

    Args:
        idea: One candidate idea sentence.
        facts: The concept's ``specific_entities``.

    Returns:
        A short reason, or ``''``.
    """
    from cqc_lem.utilities.ai.image_brief import (
        cliche_hit,
        legible_document,
        prop_failure,
        words_on_surface,
    )

    hit = cliche_hit(idea)
    if hit:
        return f"stock symbol {hit!r}"
    names = name_tokens(idea)
    if names:
        return f"names or numbers {names}"
    words = words_on_surface(idea) or legible_document(idea)
    if words:
        return f"legible words on a surface ({words!r})"
    prop = prop_failure(idea)
    if prop:
        return f"legible words on a surface ({prop})"
    lowered = {t.lower() for t in re.findall(r"[A-Za-z0-9]+", idea)}
    for fact in facts:
        if is_fact_only(fact):
            fact_names = {t.lower() for t in name_tokens(fact)} | (
                {fact.split()[0].lower()} if fact[:1].isupper() else set())
            if lowered & (fact_names - {t.lower() for t in GENERIC_ACRONYMS}):
                return f"it names the fact {fact!r}"
    return ""


def is_desk_still_life(idea: str) -> bool:
    """Is this idea an object resting on furniture with no person in it?"""
    return bool(_OBJECT_ON_FURNITURE.search(idea or "")) and not is_people_led(idea)


def is_people_led(idea: str) -> bool:
    """Does this idea put a person (and so a face) in the frame?"""
    return bool(_PEOPLE_WORDS.search(idea or ""))


def _filter_ideas(raw: Any, facts: Sequence[str]) -> tuple[str, ...]:
    ideas: list[str] = []
    for item in (raw if isinstance(raw, list) else []):
        idea = _clean(item, 280)
        if not idea or idea in ideas:
            continue
        reason = idea_rejection(idea, facts)
        if reason:
            log_debug("Visual idea dropped", idea=idea, reason=reason,
                      action_type="image_concept")
            continue
        ideas.append(idea)
    return tuple(ideas[:_VISUAL_IDEAS])


def _hook_options(payload: dict[str, Any], title: Optional[str], topic_text: str,
                  source: str) -> dict[str, str]:
    """Every valid hook Stage 1 offered, keyed by its shape (first valid per shape wins)."""
    raw = payload.get("hook_candidates")
    offered = list(raw.values()) if isinstance(raw, dict) else []
    offered += [payload.get("hook_phrase")] + list(payload.get("hook_alternatives") or [])
    options: dict[str, str] = {}
    for candidate in offered:
        hook = _valid_hook(_clean(candidate, 80), title, topic_text, source)
        if hook:
            options.setdefault(hook_shape_of(hook), hook)
    return options


def _first_valid_hook(payload: dict[str, Any], title: Optional[str],
                      topic_text: str = "", source: Optional[str] = None) -> str:
    candidates = [payload.get("hook_phrase")] + list(payload.get("hook_alternatives") or [])
    for candidate in candidates:
        hook = _valid_hook(_clean(candidate, 80), title, topic_text, source)
        if hook:
            return hook
    return ""


def _resolve_treatment(raw: str, anchors: tuple[str, ...], hook: str) -> str:
    treatment = raw if raw in TREATMENTS else TREATMENT_CONCRETE
    if treatment == TREATMENT_GRAPHIC and not hook:
        # A graphic with no usable hook is a graphic with nothing to say.
        treatment = TREATMENT_CONCRETE
    if treatment == TREATMENT_METAPHOR and len(anchors) >= _MIN_ENTITIES:
        # "Only when nothing concrete exists" — three depictable anchors say something does.
        treatment = TREATMENT_CONCRETE
    return treatment


def parse_concept(payload: Optional[dict[str, Any]], source: str,
                  title: Optional[str] = None) -> Optional[ImageConcept]:
    """Validate a raw Stage 1 reply into an ``ImageConcept``, or None when it is unusable.

    Args:
        payload: The parsed JSON object the analyst returned.
        source: The text the analyst read; entities and anchors are grounded against it.
        title: The piece's title, so a hook that only restates it can be refused.

    Returns:
        The concept, or None when there is no thesis to build an image on.
    """
    if not isinstance(payload, dict):
        return None
    thesis = gist_thesis(_clean(payload.get("thesis")))
    if not thesis:
        return None
    entities = _ground_entities(payload.get("specific_entities"), source)
    anchors = _ground_anchors(payload.get("visual_anchors"), source, entities)
    # The first hook that passes the rules, so a shouted primary hook does not cost a cover its
    # headline when a usable alternative was offered (round 3: every cover carries one).
    topic = " ".join((thesis, *entities, *anchors))
    options = _hook_options(payload, title, topic, source)
    hook = _first_valid_hook(payload, title, topic, source) or next(iter(options.values()), "")
    treatment = _resolve_treatment(_clean(payload.get("treatment"), 40).lower(), anchors, hook)
    return ImageConcept(
        thesis=thesis,
        audience=_clean(payload.get("audience"), 120),
        specific_entities=entities,
        emotional_beat=_clean(payload.get("emotional_beat"), 120),
        valence=(_clean(payload.get("valence"), 20).lower()
                 if _clean(payload.get("valence"), 20).lower() in VALENCES else "mixed"),
        hook_shape=hook_shape_of(hook) if hook else "",
        hook_options=options or None,
        kicker=valid_kicker(payload.get("kicker"), source),
        hook_phrase=hook,
        treatment=treatment,
        treatment_rationale=_clean(payload.get("treatment_rationale")),
        weak=len(anchors) < WEAK_ENTITY_FLOOR,
        visual_anchors=anchors,
        visual_ideas=_filter_ideas(payload.get("visual_ideas"), entities),
    )


def enforce_graphic_cap(concept: ImageConcept, recent_treatments: Optional[Sequence[str]],
                        surface: str) -> ImageConcept:
    """At most ONE ``editorial_graphic`` in any ``GRAPHIC_WINDOW`` consecutive covers.

    Deterministic, after Stage 1: the prompt only PREFERS variety, and gauntlet round 1 of #2241
    showed every edition choosing a graphic anyway. A capped graphic becomes a ``people_scene`` —
    the cover default.

    Args:
        concept: Stage 1's concept.
        recent_treatments: The treatments of the most recent covers, most recent first.
        surface: Only ``newsletter`` is capped.

    Returns:
        The concept, with its treatment overridden when the cap applies.
    """
    recent = [t for t in (recent_treatments or []) if t][:GRAPHIC_WINDOW - 1]
    if (surface != "newsletter" or concept.treatment != TREATMENT_GRAPHIC
            or TREATMENT_GRAPHIC not in recent):
        return concept
    log_info("Cover treatment capped — a recent cover was already a graphic",
             action_type="image_concept", recent=",".join(recent))
    return dataclasses.replace(
        concept, treatment=TREATMENT_PEOPLE,
        treatment_rationale=(f"{concept.treatment_rationale} [capped: at most one "
                             f"editorial_graphic per {GRAPHIC_WINDOW} covers]").strip())


@llm_step("image_idea_pick")
def pick_visual_idea(concept: ImageConcept, surface: str = "post_image") -> ImageConcept:
    """Rank the concept's visual ideas with ONE cheap call and keep the winner. Never raises.

    ``lem-simple`` ranks on specificity to this article, surprise, thumbnail legibility and cliché
    distance. Fails OPEN to the first surviving idea — preferring a people-led one — so an
    unreachable judge still yields a chosen idea rather than none.

    Args:
        concept: Stage 1's concept, with ``visual_ideas`` already filtered.
        surface: The surface, named to the ranker.

    Returns:
        The concept with ``chosen_idea``, ``rejected_ideas`` and ``idea_pick_reason`` set; the
        concept unchanged when it has no ideas.
    """
    ideas = list(concept.visual_ideas)
    if not ideas:
        return concept
    default = next((i for i in ideas if is_people_led(i)), ideas[0])
    winner, reason = default, "ranking unavailable — first people-led idea"
    if len(ideas) > 1:
        from cqc_lem.utilities.ai.ai_helper import _loads_json_object
        from cqc_lem.utilities.ai.client import client

        try:
            response = client.chat.completions.create(
                model="lem-simple",
                messages=[{"role": "user", "content": _IDEA_PICK_PROMPT.format(
                    surface=surface, thesis=concept.thesis, hook=concept.hook_phrase or "(none)",
                    ideas="\n".join(f"{n}. {idea}" for n, idea in enumerate(ideas, 1)))}],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=_IDEA_PICK_MAX_TOKENS,
                reasoning_effort=REASONING_EFFORT,
            )
            verdict = _loads_json_object(response.choices[0].message.content or "") or {}
            ranking = [int(n) for n in (verdict.get("ranking") or [])
                       if isinstance(n, (int, float, str)) and str(n).strip().isdigit()]
            ranked = [ideas[n - 1] for n in ranking if 1 <= n <= len(ideas)]
            if ranked:
                winner = ranked[0]
                reason = _clean(verdict.get("reason"), 200) or "ranked first"
                if is_desk_still_life(winner):
                    # Deterministic backstop: a people-led idea beats an object on a desk.
                    people = [i for i in ranked if is_people_led(i)]
                    if people:
                        winner = people[0]
                        reason = "object-on-a-desk idea demoted for the best people-led one"
        except Exception as e:
            log_debug("Visual idea ranking unavailable — taking the default", error=str(e),
                      action_type="image_concept")
    else:
        reason = "only one idea survived the filters"
    return dataclasses.replace(concept, chosen_idea=winner,
                               rejected_ideas=tuple(i for i in ideas if i != winner),
                               idea_pick_reason=reason)


# Round 6 (#2241): all four covers shared one composition — a dark panel left, a reacting man
# in his 30s-40s right. Layout and cast now rotate DETERMINISTICALLY, least-recently-used across
# the author's last ``ROTATION_WINDOW`` receipts, as a sibling of ``enforce_graphic_cap``.
ROTATION_WINDOW = 4
# Compositing templates (``image_compose.LAYOUTS``): the headline is typeset, never rendered.
# ``full_bleed`` is OUT of the rotation (round 7: it set a headline across a woman's face); the
# compositor still supports it for a caller that asks.
COVER_LAYOUTS = ("panel_left", "panel_right", "lower_third_band")
# A 4:5 post keeps its headline in a band at the top, below a margin the crop protects.
POST_LAYOUTS = ("band_top",)
SURFACE_LAYOUTS = {"newsletter": COVER_LAYOUTS, "post_image": POST_LAYOUTS}
# Each dimension rotates INDEPENDENTLY, so no pairing (of age, gender, ethnicity or setting) is
# ever correlated with another — or with the role, which always comes from the piece's anchors.
CAST_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "gender": ("woman", "man"),
    "age": ("20s", "30s", "40s", "50s", "60s"),
    "ethnicity": ("Black", "East Asian", "South Asian", "Hispanic", "white", "Middle Eastern",
                  "Southeast Asian"),
    "setting": ("office", "home office", "warehouse floor", "shop counter", "conference hallway"),
}
_CAST_SURFACES = frozenset({"newsletter", "post_image", "video"})
_ROLE_WORDS = re.compile(
    r"\b(?:owner|founder|lead|manager|editor|analyst|marketer|consultant|director|officer|"
    r"executive|reviewer|buyer|accountant|seller|engineer|designer|writer|recruiter|coach|"
    r"strategist|operator|planner|specialist)s?\b", re.IGNORECASE)


def _least_recent(options: Sequence[str], recent: Sequence[str], seed: str) -> str:
    """The option unused in ``recent`` (most recent first), else the one used longest ago.

    With no history at all, a stable hash of ``seed`` picks — so surfaces with no receipt reader
    still vary piece to piece instead of always taking the first option.
    """
    recent = [r for r in recent if r in options]
    if not recent:
        return options[sum(map(ord, seed or "")) % len(options)]
    unused = [o for o in options if o not in recent]
    if unused:
        return unused[0]
    return max(options, key=recent.index)


def _role_from_anchors(concept: ImageConcept) -> str:
    """The person's role, drawn from the piece's own anchors (then its audience) — never invented.

    "an agency owner" gives "agency owner", "engineering lead reviewing billing" gives
    "engineering lead", an audience of "agency owners" gives "agency owner".
    """
    for text in tuple(concept.visual_anchors) + (concept.audience,):
        words = (text or "").split()
        for idx, word in enumerate(words):
            if _ROLE_WORDS.fullmatch(word.strip(",.;:")):
                start = max(0, idx - 1)
                if words[start].lower() in ("a", "an", "the"):
                    start = idx
                role = " ".join(words[start:idx + 1]).lower().strip(",.;:")
                return role[:-1] if role.endswith("s") and not role.endswith("ss") else role
    return "professional"


_SETTING_PREPOSITION = {"warehouse floor": "on", "shop counter": "behind",
                        "conference hallway": "in"}


def cast_phrase(cast: Optional[dict[str, str]]) -> str:
    """The brief's cast line: "a {ethnicity} {gender} in her/his {age}, a {role}, at a {setting}".

    Args:
        cast: The rotated cast dict, or None.

    Returns:
        The phrase, or '' without a cast.
    """
    if not cast:
        return ""
    pronoun = "her" if cast.get("gender") == "woman" else "his"
    preposition = _SETTING_PREPOSITION.get(cast["setting"], "in")
    role = cast["role"]
    article = "an" if role[:1].lower() in "aeiou" else "a"
    return (f"a {cast['ethnicity']} {cast['gender']} in {pronoun} {cast['age']}, {article} "
            f"{role}, {preposition} a {cast['setting']}")


def rotate_hook_shape(concept: ImageConcept,
                      recent_shapes: Optional[Sequence[str]] = None) -> ImageConcept:
    """Pick the least-recently-used hook SHAPE among the valid hooks Stage 1 offered.

    Args:
        concept: Stage 1's concept, with ``hook_options``.
        recent_shapes: Shapes of the most recent hooks, most recent first.

    Returns:
        The concept with ``hook_phrase`` and ``hook_shape`` set to the rotated pick; unchanged
        when it offered fewer than two shapes.
    """
    options = concept.hook_options or {}
    if len(options) < 2:
        return concept
    shapes = tuple(s for s in HOOK_SHAPES if s in options)
    shape = _least_recent(shapes, list(recent_shapes or [])[:ROTATION_WINDOW], concept.thesis)
    return dataclasses.replace(concept, hook_phrase=options[shape], hook_shape=shape)


def assign_layout_and_cast(concept: ImageConcept, surface: str,
                           recent_layouts: Optional[Sequence[str]] = None,
                           recent_casts: Optional[Sequence[dict]] = None) -> ImageConcept:
    """Rotate the composition and (for a people_scene) the person, least-recently-used.

    Args:
        concept: Stage 1's concept.
        surface: Only surfaces with a layout table get a layout; covers, posts and video frames
            get a cast.
        recent_layouts: The last ``ROTATION_WINDOW`` layouts on this surface, most recent first.
        recent_casts: The last ``ROTATION_WINDOW`` cast dicts, most recent first.

    Returns:
        The concept with ``layout`` and ``cast`` set (unchanged values when not applicable).
    """
    seed = concept.thesis
    layout = concept.layout
    options = SURFACE_LAYOUTS.get(surface)
    if options and not layout:
        layout = _least_recent(options, list(recent_layouts or [])[:ROTATION_WINDOW], seed)
    cast = concept.cast
    if surface in _CAST_SURFACES and concept.treatment == TREATMENT_PEOPLE and not cast:
        history = [c for c in (recent_casts or []) if isinstance(c, dict)][:ROTATION_WINDOW]
        cast = {dim: _least_recent(values, [h.get(dim, "") for h in history], seed + dim)
                for dim, values in CAST_DIMENSIONS.items()}
        cast["role"] = _role_from_anchors(concept)
    return dataclasses.replace(concept, layout=layout, cast=cast)


@llm_step("image_concept")
def analyze_content_for_image(text: str, *, title: Optional[str] = None,
                              surface: str = "post_image",
                              user_id: Optional[int] = None,
                              recent_treatments: Optional[Sequence[str]] = None,
                              recent_layouts: Optional[Sequence[str]] = None,
                              recent_casts: Optional[Sequence[dict]] = None,
                              recent_hook_shapes: Optional[Sequence[str]] = None,
                              ) -> Optional[ImageConcept]:
    """Read the full content and decide what its image must show. Never raises.

    One ``lem-medium`` JSON call over up to ``_MAX_SOURCE_CHARS`` of ``text``.

    Args:
        text: The FULL content — a whole edition body, not its hook.
        title: The piece's title, when it has one.
        surface: The surface the image is for. ``newsletter`` adds the cover bias toward people
            and the deterministic graphic cap.
        user_id: The author, for log context.
        recent_treatments: Treatments of this author's most recent images, most recent first;
            the analyst is asked to prefer a different one when the piece allows.
        recent_layouts: Layouts of the most recent images on this surface, most recent first.
        recent_casts: Cast dicts of the most recent images, most recent first.
        recent_hook_shapes: Hook shapes of the most recent images, most recent first.

    Returns:
        The concept, or None when the call fails or returns nothing usable — callers keep a
        deterministic path for exactly that case.
    """
    from cqc_lem.utilities.ai.ai_helper import _loads_json_object
    from cqc_lem.utilities.ai.client import client

    source = "\n\n".join(p for p in (title, text) if p and p.strip())[:_MAX_SOURCE_CHARS]
    if not source.strip():
        return None
    recent = [t for t in (recent_treatments or []) if t]
    surface_guidance = {"newsletter": _COVER_GUIDANCE, "post_image": _POST_GUIDANCE}
    guidance = (surface_guidance[surface] + "\n" if surface in surface_guidance else "") + (
        f"Recent images by this author used, most recent first: {', '.join(recent)}. Prefer a "
        f"different treatment than {recent[0]} when the piece allows.\n" if recent else "")
    try:
        payload = None
        for _attempt in (1, 2):
            response = client.chat.completions.create(
                model="lem-medium",
                messages=[{"role": "system", "content": _SYSTEM_PROMPT},
                          {"role": "user", "content": (
                              f"Surface: {surface}\n{guidance}"
                              f"<title>{title or ''}</title>\n<content>{source}</content>")}],
                response_format={"type": "json_object"},
                temperature=0.3,
                max_tokens=_CONCEPT_MAX_TOKENS,
                reasoning_effort=REASONING_EFFORT,
            )
            choice = response.choices[0]
            raw = choice.message.content or ""
            # ONE more try when the budget, not the model, ended the reply.
            if not raw.strip() and getattr(choice, "finish_reason", None) == "length":
                log_debug("Image concept cut off at the token budget — retrying once",
                          user_id=user_id, surface=surface, action_type="image_concept")
                continue
            payload = _loads_json_object(raw)
            break
    except Exception as e:
        # Expected degradation, not a defect: the brief author has a path without a concept.
        log_debug("Image concept analysis unavailable", error=str(e), user_id=user_id,
                  surface=surface, action_type="image_concept")
        return None
    concept = parse_concept(payload, source, title)
    if concept is None:
        log_debug("Image concept reply unusable", user_id=user_id, surface=surface,
                  action_type="image_concept", raw=json.dumps(payload)[:200] if payload else "")
        return None
    concept = pick_visual_idea(enforce_graphic_cap(concept, recent, surface), surface)
    concept = rotate_hook_shape(concept, recent_hook_shapes)
    return assign_layout_and_cast(concept, surface, recent_layouts, recent_casts)
