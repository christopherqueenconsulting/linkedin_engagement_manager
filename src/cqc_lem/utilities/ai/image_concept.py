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
# Archetype round (#2241): the AI render for an ``editorial_concept`` — one surprising juxtaposition
# of everyday objects, no people. Set by ``select_archetype``, never offered to the analyst.
TREATMENT_EDITORIAL = "editorial_concept"

# The seven visual archetypes (``docs/visual-archetypes-research.md`` §6.2). The first five are
# drawn by ``image_graphics`` from the piece's own verified numbers and steps; the last two are AI
# renders beside the typeset panel. A people scene is a HUMAN MOMENT only, never the default.
ARCHETYPE_STAT_CARD = "stat_card"
ARCHETYPE_HIGHLIGHT_CHART = "highlight_chart"
ARCHETYPE_RECEIPT = "receipt"
ARCHETYPE_BEFORE_AFTER = "before_after"
ARCHETYPE_CHECKLIST = "checklist"
ARCHETYPE_EDITORIAL = "editorial_concept"
ARCHETYPE_PEOPLE = "people_scene"
CODE_DRAWN_ARCHETYPES = (ARCHETYPE_STAT_CARD, ARCHETYPE_HIGHLIGHT_CHART, ARCHETYPE_RECEIPT,
                         ARCHETYPE_BEFORE_AFTER, ARCHETYPE_CHECKLIST)
AI_ARCHETYPES = (ARCHETYPE_EDITORIAL, ARCHETYPE_PEOPLE)
ARCHETYPES = CODE_DRAWN_ARCHETYPES + AI_ARCHETYPES
# Only the composited surfaces choose an archetype; every other surface keeps its treatment.
ARCHETYPE_SURFACES = frozenset({"newsletter", "post_image"})
# Selection (§6.3): evidence strength first. A chart, a receipt or a before/after needs MORE
# verified data than a single stat, so it is the more specific image when the piece has it.
ARCHETYPE_BASE_SCORES = {ARCHETYPE_HIGHLIGHT_CHART: 4.0, ARCHETYPE_RECEIPT: 4.0,
                         ARCHETYPE_BEFORE_AFTER: 4.0, ARCHETYPE_STAT_CARD: 3.0,
                         ARCHETYPE_CHECKLIST: 3.0, ARCHETYPE_PEOPLE: 2.5,
                         ARCHETYPE_EDITORIAL: 1.0}
# People is 2.5, not 3 (#2241 showcase): at 3 the analyst's +0.5 tiebreak let a people scene
# OUTRANK a validated checklist or stat card (3.5 > 3) on every human-moment edition, so no
# cover drew its evidence. At 2.5 the tiebreak can at most TIE a 3.0 graphic, and ties go
# code-drawn first — a people photo wins only when no graphic validated or one is rotated out.
# The analyst's own pick only BREAKS a tie; it never outranks a stronger evidence score.
ARCHETYPE_TIEBREAK = 0.5
# Never the same archetype as either of the last two images (§6.3 step 4). The research's -2
# let a recent chart (4 - 2) still beat a fresh editorial concept (1), which is a repeat, not
# avoidance; the penalty is larger than any gap between base scores, so a recent archetype loses
# to EVERY fresh candidate and only renders when nothing else can.
ARCHETYPE_WINDOW = 2
ARCHETYPE_ROTATION_PENALTY = 4.0
# The text-free art styles an editorial_concept rotates through, least-recently-used. Phrased
# without "paper", "printed" or "illustration": the brief's prop and vocabulary rules refuse those.
ART_STYLES: dict[str, str] = {
    "risograph": ("a vintage risograph print in two spot inks, charcoal and mustard gold, with "
                  "visible grain and slight misregistration on a flat off-white ground"),
    "cut_collage": ("a torn-edge cut-out collage with halftone photo fragments and soft drop "
                    "shadows on a flat off-white ground"),
    "claymation": ("a tactile claymation miniature set with fingerprinted clay surfaces, shot "
                   "with a macro lens under soft studio light on a seamless warm-grey ground"),
    "editorial_photo": ("a single-object editorial still-life photograph on a seamless "
                        "colour-blocked backdrop, hard flash, from an unusual overhead angle"),
}

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
 "setting": "<WHERE the story happens, from the article: its own scene ('a billing review late \
on a Tuesday', 'a conference hallway'), else the audience's real workplace the text implies>",
 "idea_nouns": ["<up to 20 concrete nouns from the piece and the reader's working day>"],
 "visual_ideas": ["<3 one-sentence object-only image ideas>"],
 "people_idea": "<ONE one-sentence people-led idea, for a human moment only>",
 "human_moment": true|false,
 "treatment": "{'|'.join(TREATMENTS)}",
 "treatment_rationale": "<one sentence>",
 "archetype": "{'|'.join(ARCHETYPES)}",
 "graphic_facts": {{
   "source_name": "<the publication, study or report the piece names for its numbers, copied \
exactly, or ''>",
   "thesis_stat": {{"label": "<what the number measures, words from its sentence>",
                    "value": "<the number exactly as written, e.g. 45 or 30,000>",
                    "unit": "<$ or € or £, %, x, a unit word such as hours, or ''>",
                    "source_sentence": "<the sentence it appears in, copied verbatim>"}} | null,
   "comparison": {{"measure": "<what all the items measure>", "items": [<2-6 items shaped like \
thesis_stat, ALL on that same measure and unit>], "highlight": <index of the item the piece is \
about>, "annotation": "<at most 8 words from the piece>"}} | null,
   "costs": {{"subject": "<X in 'the real cost of X', 1-4 words from the piece>", "items": \
[<2-5 cost or time items shaped like thesis_stat, labels at most 5 words>], "total": <an item, \
ONLY if the piece states a total> | null}} | null,
   "before_after": {{"before": <item>, "after": <item, same measure and unit>}} | null,
   "steps": [{{"text": "<a step or recommendation in at most 8 of the piece's words>", \
"source_sentence": "<copied verbatim>"}}]}}}}

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
on AI posts"), contrast ("Search is on, still wrong"), question ("Who really buys your AI?"), \
plain_claim ("Your cheapest model is enough") — the series rotates through them. The hook \
carries the THESIS; the image carries emotion and specificity. A hook is a CLAIM with a verb \
("Routing cuts spend 60%"), never a descriptive label ("Routing prompts by complexity"), never \
a caveat and never "X vs Y". When the piece leads with a stat, the hook carries it verbatim.

Rules for valence: positive when the piece is about a saving, a win or relief; negative when it \
is about a risk, a loss or a mistake; mixed otherwise. The face in the image follows it.

Rules for setting: the place comes from the ARTICLE, never a stock backdrop — the story's own \
scene when it has one, else the real workplace of its audience as the text describes it. A few \
words, lowercase, with its article ("a client's open-plan office"). Never a warehouse or a \
storeroom unless the piece is about one.

Rules for kicker: the 1-3 word topic tag a magazine prints above a headline — "AI CONTENT AUDIT", \
"LLM COSTS", "AI FACT-CHECKING", "B2B BUYING". Every word comes from the article. It is what tells \
a scroller what the cover is ABOUT, so the scene never has to.

Rules for visual_ideas — the Idea Miner. (1) Hold the thesis. (2) List up to 20 concrete nouns \
in idea_nouns, from the piece and from the reader's working day: tools, containers, furniture, \
vehicles, rooms, everyday objects — never paper, documents, screens or a person. (3) Pair them \
with the piece's tension using ONE operator: juxtaposition, scale shift, a visual oxymoron, a \
literalised idiom, or a transformation in progress — two objects side by side beat one fused \
hybrid. (4) Drop every cliché: gears, lightbulbs, puzzle pieces, robots, brains, rockets, chess, \
handshakes, targets, magnifying glasses, clouds, binary code, glowing networks. (5) Keep the 3 a \
stranger would "get" within two seconds with the headline and that could NOT illustrate the \
opposite argument. Each idea is ONE sentence naming its objects and its single oddity, with NO \
person, face or hand in it — for "your CRM leaks leads": "A galvanised bucket with holes \
punched in its side, brass door keys spilling out across the floor, seen from overhead." No \
names, no numbers, no stock symbols, no cards, tokens or figurines.

Rules for people_idea and human_moment: human_moment is true ONLY when the piece is about a human \
moment — a hire, a hard conversation, a client's reaction, a founder's own decision — where a \
face IS the story; false for analysis, numbers, tools, processes and frameworks. people_idea is \
that moment as one sentence: a specific person, a specific readable reaction, their gaze turned \
toward the side of the frame — never at the camera, never at a laptop.

Rules for graphic_facts: the evidence a data graphic can draw. Copy every value and every \
source_sentence EXACTLY from the text; a number the text does not state, a baseline it does not \
give, a total it does not state, or a sum you computed is FORBIDDEN — leave the field null \
instead. Every word of a label comes from its own sentence. comparison only when the piece \
gives 2+ numbers on the SAME measure (45% vs 38.2% engagement). steps only when the piece gives \
3+ steps or recommendations.

Rules for archetype: your one pick for the image. It only breaks a tie between the evidence the \
system has already verified, so pick by what would make a stranger stop: a stat_card for one \
surprising number, a highlight_chart for a comparison, a receipt for costs, before_after for a \
transformation, a checklist for steps, an editorial_concept for an abstract claim, a \
people_scene only for a human moment.

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
    "This is a newsletter COVER. It carries the piece's IDEA or its EVIDENCE — readers skip "
    "anonymous stock people as filler, so a people_scene is for a human moment only. Fill "
    "graphic_facts with every verifiable number and step. A cover ALWAYS carries its hook as a "
    "headline, so hook_phrase is required. Never an object sitting on a desk.")

# Round 5: the post image follows the cover recipe — a hook, a face, a bright scene.
_POST_GUIDANCE = (
    "This is a LinkedIn feed POST image (4:5). It carries the post's IDEA or its EVIDENCE — a "
    "people_scene only for a human moment, never as the default. Fill graphic_facts with every "
    "verifiable number and step. The image ALWAYS carries its hook as a headline, so "
    "hook_phrase is required. Never an object sitting on a desk.")
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


# Archetype round: an editorial_concept's ideas are object-only juxtapositions, ranked on the
# "piques interest" rubric (docs/visual-archetypes-research.md §6.4) instead of a face.
_EDITORIAL_PICK_PROMPT = """You are picking ONE editorial-cover idea for a LinkedIn {surface}.

The piece argues: {thesis}
Its headline (hook): {hook}

Candidate ideas:
{ideas}

Score each 0-2 on: thumbnail read (one focal point at 400x225), thesis fit (it could NOT \
illustrate the opposite argument), curiosity gap (shows part, withholds part), novelty (an \
object from the reader's own world in an unexpected pairing, never a stock symbol), resolves \
within 2 seconds with the headline, and relevance to a small-business owner's money, time or \
risk. Rank by total. Respond with ONLY a JSON object:
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
        archetype: The visual archetype the image takes (``ARCHETYPES``); '' off the composited
            surfaces.
        archetype_ranking: The fallback chain, best first, ending at the first AI archetype — a
            code-drawn graphic that cannot be drawn falls to the next.
        archetype_rationale: Every candidate's score, for the receipt.
        archetype_hint: The analyst's own pick — a tiebreak only.
        graphic: The VALIDATED graphic facts (``image_graphics.validate_graphic_facts``): every
            figure with the source sentence it was verified against.
        human_moment: The analyst's call that a face IS the story; the only way to a people scene.
        people_idea: The one people-led idea, used only when the archetype is a people scene.
        idea_nouns: The Idea Miner's nouns, kept for the receipt.
        art_style: The rotated ``ART_STYLES`` key an editorial_concept renders in.
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
    setting: str = ""
    shot: str = ""
    archetype: str = ""
    archetype_ranking: tuple[str, ...] = ()
    archetype_rationale: str = ""
    archetype_hint: str = ""
    graphic: Optional[dict[str, Any]] = None
    human_moment: bool = False
    people_idea: str = ""
    idea_nouns: tuple[str, ...] = ()
    art_style: str = ""

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
# Round 9 (#2241): words a derived kicker never uses — they name no topic.
_KICKER_SKIP = frozenset({
    "about", "after", "also", "because", "been", "before", "being", "between", "both", "does",
    "doing", "each", "every", "have", "having", "here", "just", "like", "many", "more", "most",
    "much", "only", "other", "over", "same", "should", "some", "such", "than", "then", "there",
    "these", "they", "this", "those", "through", "very", "what", "when", "where", "which",
    "while", "will", "with", "would", "your", "yours", "into", "them", "were", "make", "made",
    "really", "still", "even", "back", "well", "good", "better", "best", "need", "needs",
})


def derive_kicker(source: str, title: Optional[str] = None) -> str:
    """A deterministic kicker for when Stage 1's fails ``valid_kicker``: never ''.

    The 1-2 most frequent topic words of the piece, title words first (a title word outranks any
    body word), uppercased. A word is 4+ letters, or a ``_KICKER_ACRONYMS`` acronym of any length,
    so "AI" survives where "the" never does. Two words only when the second also recurs.

    Args:
        source: The analysed text.
        title: The piece's title, if any.

    Returns:
        The uppercase kicker; "INSIGHT" when the piece has no usable word at all.
    """
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    in_title = set()
    for position, token in enumerate(re.findall(r"[a-z0-9]+", f"{title or ''} {source or ''}"
                                                .lower())):
        if token.isdigit() or token in _KICKER_SKIP or token in _STOPWORDS:
            continue
        if len(token) < 4 and token not in _KICKER_ACRONYMS:
            continue
        counts[token] = counts.get(token, 0) + 1
        first_seen.setdefault(token, position)
    for token in re.findall(r"[a-z0-9]+", (title or "").lower()):
        if token in counts:
            in_title.add(token)
    ranked = sorted(counts, key=lambda t: (t not in in_title, -counts[t], first_seen[t]))
    if not ranked:
        return "INSIGHT"
    chosen = ranked[:1]
    if len(ranked) > 1 and (counts[ranked[1]] >= 2 or ranked[1] in in_title):
        chosen.append(ranked[1])
    chosen.sort(key=lambda t: first_seen[t])
    return " ".join(chosen).upper()


def concept_kicker(concept: Any) -> str:
    """The concept's kicker, derived from its own thesis and hook when Stage 1 gave none.

    Args:
        concept: An ``ImageConcept`` (or None).

    Returns:
        The kicker; '' only when there is no concept.
    """
    if concept is None:
        return ""
    kicker = str(getattr(concept, "kicker", "") or "")
    if kicker:
        return kicker
    # The hook is body text here, never a title: "Who pays?" must not outrank the thesis topic.
    return derive_kicker(" ".join((getattr(concept, "thesis", "") or "",
                                   *(getattr(concept, "visual_anchors", ()) or ()),
                                   getattr(concept, "hook_phrase", "") or "")))



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


def hook_rejection(hook: str, title: Optional[str], topic_text: str = "",
                   source: Optional[str] = None) -> str:
    """Why ``hook`` is not a usable curiosity gap, or '' when it is.

    2-6 words, at most ``_HOOK_MAX_CHARS``, no exclamation mark, no imperative opener, not Title
    Case, not the title again — a hook with a number in it must name its subject
    (``names_its_subject``), every word must come from the piece, and a comparative needs its
    reference (``vague_comparative``).

    Args:
        hook: The candidate hook.
        title: The piece's title.
        topic_text: Thesis, facts and anchors — what a number's subject may be named from.
        source: The analysed text, for faithfulness; None skips that check.

    Returns:
        The reason, phrased for the analyst; '' when the hook passes.
    """
    hook = (hook or "").strip().strip("\"'“”‘’").strip()
    words = hook.split()
    if not (_HOOK_MIN_WORDS <= len(words) <= _HOOK_MAX_WORDS) or len(hook) > _HOOK_MAX_CHARS:
        return f"{_HOOK_MIN_WORDS}-{_HOOK_MAX_WORDS} words, at most {_HOOK_MAX_CHARS} characters"
    if "!" in hook or words[0].lower().strip(",.:;") in _IMPERATIVE_OPENERS:
        return "an order or a shout is an ad, not a curiosity gap"
    if is_title_case(hook):
        return "sentence case only, never Title Case"
    if _VERSUS.search(hook):
        return "an 'X vs Y' hook takes no side — state the piece's claim"
    if not any(ch.isdigit() for ch in hook) and not asserts_something(hook):
        return "a descriptive label, not a claim — the hook needs a verb that asserts something"
    hook_tokens = set(_content_tokens(hook))
    title_tokens = set(_content_tokens(title or ""))
    if hook_tokens and title_tokens and (
            len(hook_tokens & title_tokens) / len(hook_tokens) >= _HOOK_TITLE_OVERLAP):
        return "it restates the title"
    if any(ch.isdigit() for ch in hook) and not names_its_subject(
            hook, f"{title or ''} {topic_text}"):
        return "a number must name its subject (45% less reach for AI posts)"
    if source is not None and not hook_is_faithful(hook, source):
        return "it uses a number or word the piece never says"
    if source is not None:
        mismatch = number_claim_mismatch(hook, source)
        if mismatch:
            return mismatch
    comparative = vague_comparative(hook)
    if comparative:
        return f"{comparative!r} needs its reference: say 'than …' or give the number"
    return ""


# Round 13: "Routing prompts to models by complexity" is a label, not a claim. A hook (other than
# a number_claim) needs a verb that ASSERTS something. Deterministic and conservative: a copula
# or modal, a known business verb in any inflection, or a past-tense -ed form; a question passes.
_ASSERTING = frozenset({
    "is", "are", "was", "were", "be", "been", "isn", "aren", "wasn", "weren", "s", "can", "cannot",
    "could", "will", "won", "would", "should", "must", "might", "may", "do", "does", "did", "don",
    "doesn", "didn", "has", "have", "had", "hasn", "haven", "ll", "re", "ve", "d",
})
_VERBS = frozenset({
    "beat", "break", "bring", "build", "buy", "catch", "change", "cost", "cut", "decide", "die",
    "drain", "drive", "drop", "earn", "eat", "end", "fail", "fall", "find", "fix", "get", "give",
    "go", "grow", "hide", "hit", "hold", "hurt", "keep", "kill", "know", "lag", "land", "last",
    "lead", "leak", "learn", "leave", "lie", "lift", "lose", "make", "matter", "mean", "miss",
    "move", "need", "outperform", "owe", "pay", "pick", "prove", "pull", "push", "quit", "raise",
    "read", "rely", "rise", "rule", "run", "save", "say", "see", "sell", "send", "shift", "ship",
    "show", "shrink", "sink", "skip", "slip", "slow", "solve", "spend", "stall", "starve", "stay",
    "steal", "stop", "take", "talk", "tell", "think", "trust", "turn", "use", "waste", "want",
    "win", "work", "write", "beats", "wins", "matters", "decides", "pays", "buys", "cuts", "saves",
    "hurts", "kills", "costs", "breaks", "leaks", "lies", "outsell", "underperform", "doubt",
    "fear", "ignore", "forget", "outgrow", "replace", "eats", "drains", "fails", "loses",
    "misses", "needs", "works", "lose", "sees", "says", "knows", "wants", "uses", "runs",
    "grows", "falls", "rises", "drops", "shows", "proves", "makes", "takes", "gets", "keeps",
    "finds", "spends", "wastes", "starves", "stalls", "slips", "holds", "lands", "earns",
})


def asserts_something(hook: str) -> bool:
    """Does ``hook`` carry a verb that asserts something (round 13)?

    Args:
        hook: The hook.

    Returns:
        True for a question, a copula or modal, a known verb in any inflection, or an -ed past
        form; False for a noun-phrase label ("Routing prompts to models by complexity").
    """
    if (hook or "").rstrip().endswith("?"):
        return True
    for token in re.findall(r"[a-z]+", (hook or "").lower().replace("’", "'")):
        if token in _ASSERTING or token in _VERBS or _root(token) in _VERBS:
            return True
        if len(token) > 4 and token.endswith("ed") and token not in ("unused", "need"):
            return True
    return False


# Round 12: "Cheapest model everywhere vs hybrid routing" takes no side; the critic wants a claim.
_VERSUS = re.compile(r"\b(?:vs|versus|v)\b\.?", re.IGNORECASE)


# Round 14 (#2241): ed18 shipped "53.7% less engagement than humans" — 53.7% was the SHARE of
# AI-generated posts; the engagement gap was 45%. A number in a hook is true only when the number
# AND the claim it is attached to occur in the SAME source sentence.
_HOOK_NUMBER = re.compile(r"[$€£]?\d[\d,.]*(?:%|[kKmMbB]\b|[xX×](?!\w))?")
_CLAIM_SKIP = frozenset({
    "less", "more", "fewer", "lower", "higher", "of", "the", "a", "an", "in", "on", "for", "to",
    "by", "than", "about", "around", "nearly", "over", "under", "up", "down", "per", "cent",
    "percent", "faster", "cheaper", "times", "x", "k", "m", "b",
})
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def source_sentences(source: str) -> list[str]:
    """The source split into sentences (a title line counts as one).

    Args:
        source: The analysed text.

    Returns:
        The non-empty sentences, in order.
    """
    return [s.strip() for s in _SENTENCE_SPLIT.split(source or "") if s.strip()]


def claim_noun(hook: str, number: str) -> str:
    """The word ``number`` quantifies in ``hook``: the first content word after it, else before.

    "45% less engagement on AI posts" gives "engagement"; "Routing cut spend by 60%" gives
    "spend". Comparatives, prepositions, function words and verbs are skipped.

    Args:
        hook: The hook.
        number: A number as it appears in the hook.

    Returns:
        The lowercase claim word, or '' when the hook has none.
    """
    lowered = (hook or "").lower()
    at = lowered.find(number.lower())
    if at < 0:
        return ""

    def usable(token: str) -> bool:
        return (len(token) >= 3 and token not in _CLAIM_SKIP and token not in _STOPWORDS
                and token not in _HOOK_FREE_WORDS)

    # After the number, a verb is never its subject ("60% cuts spend"); before it, the nearest
    # content word is ("Routing cut spend by 60%" — "spend" is the noun here, not the verb).
    after = re.findall(r"[a-z]+", lowered[at + len(number):])
    for token in after:
        if usable(token) and token not in _VERBS and _root(token) not in _VERBS:
            return token
    before = re.findall(r"[a-z]+", lowered[:at])
    for token in reversed(before):
        if usable(token):
            return token
    return ""


def number_claim_mismatch(hook: str, source: str) -> str:
    """Why a number in ``hook`` is paired with a claim its source sentence does not make, or ''.

    Args:
        hook: The hook.
        source: The analysed text.

    Returns:
        The reason, or '' when every number shares a source sentence with its claim word.
    """
    sentences = source_sentences(source)
    for match in _HOOK_NUMBER.finditer(hook or ""):
        number = match.group(0).rstrip(".,")
        noun = claim_noun(hook, number)
        if not noun:
            continue
        holders = [s.lower() for s in sentences if number.lower() in s.lower()]
        if not holders:
            return f"{number!r} never appears in the source"
        if not any(re.search(rf"\b{re.escape(_root(noun))}", s) for s in holders):
            return (f"{number!r} and {noun!r} never share a source sentence — that number "
                    f"measures something else; use the number the source gives for {noun!r}, "
                    f"or no number")
    return ""


def cited_sentence(hook: str, source: str) -> str:
    """The source sentence a hook cites: the one holding its number, else the closest by words.

    Args:
        hook: The hook.
        source: The analysed text.

    Returns:
        The sentence, or '' without a source.
    """
    sentences = source_sentences(source)
    if not sentences:
        return ""
    for match in _HOOK_NUMBER.finditer(hook or ""):
        number = match.group(0).rstrip(".,").lower()
        noun = claim_noun(hook, number)
        holders = [s for s in sentences if number in s.lower()]
        if noun:
            holders = [s for s in holders if re.search(rf"\b{re.escape(_root(noun))}",
                                                       s.lower())] or holders
        if holders:
            return holders[0]
    tokens = set(_content_tokens(hook))
    return max(sentences, key=lambda s: len(tokens & set(_content_tokens(s))))


_PRONOUN_CONTRACTIONS = frozenset({"it's", "that's", "what's", "who's", "here's", "there's",
                                   "he's", "she's", "nobody's", "everyone's"})
_PREPOSITIONS = frozenset({"to", "by", "for", "with", "on", "in", "of", "at", "from", "into",
                           "across", "over", "under", "through", "without", "per", "via",
                           "than", "because", "while", "when", "that", "which"})
_FILLER = frozenset({"the", "a", "an", "everywhere", "anywhere", "always", "often", "now",
                     "today", "again", "too", "very", "really", "just", "still"})
_ADVERB_KEEP = frozenset({"only", "early", "family", "supply", "apply", "daily", "rely",
                          "reply", "fly", "ally", "italy", "july"})


def _is_verb_word(word: str) -> bool:
    """Is this single word an asserting verb (copula, modal, known verb, or -ed form)?"""
    w = word.lower().strip(".,;:!?\"'“”‘’")
    if w in _PRONOUN_CONTRACTIONS or w.split("'")[0] in ("isn", "aren", "don", "doesn",
                                                         "didn", "can", "won", "wasn"):
        return True
    if "'" in w or "’" in w:
        return False  # "AI's" is a possessive, not "is"
    if w in _ASSERTING - {"s", "d", "ll", "re", "ve"} or w in _VERBS:
        return True
    for cut in (1, 2, 3):
        if len(w) - cut >= 3 and (w[:-cut] in _VERBS or f"{w[:-cut]}e" in _VERBS):
            if w.endswith(("s", "ed", "es", "ies")):
                return True
    return len(w) > 4 and w.endswith("ed") and w not in ("unused", "need", "speed")


def _clause_words(thesis: str) -> list[str]:
    clause = _CLAUSE_BREAK.split(thesis or "", maxsplit=1)[0]
    words = [w.strip(".!?\"“”") for w in clause.split()]
    words = [w for w in words if w]
    if words and words[0].lower() in ("a", "an", "the"):
        words = words[1:]
    # "-ly" adverbs carry no claim and cost words ("raises deal costs dramatically").
    return [w for w in words if not (len(w) > 4 and w.lower().endswith("ly")
                                     and w.lower() not in _ADVERB_KEEP)]


def _fits(words: Sequence[str]) -> bool:
    text = " ".join(words)
    return _HOOK_MIN_WORDS <= len(words) <= _HOOK_MAX_WORDS and len(text) <= _HOOK_MAX_CHARS


def _noun_phrase(words: Sequence[str]) -> list[str]:
    """The words up to the first preposition or auxiliary — the phrase's own noun group.

    Only an auxiliary ends it: "deal costs" and "AI spend" are nouns here, not verbs.
    """
    out: list[str] = []
    for w in words:
        if out and (w.lower() in _PREPOSITIONS or w.lower().strip(".,") in _ASSERTING):
            break
        out.append(w)
    return out


def clause_hook(thesis: str) -> str:
    """A COMPLETE clause of at most ``_HOOK_MAX_WORDS`` words built from the thesis (round 15).

    Never a truncation: "Ignoring AI's hidden buyers raises deal costs dramatically" becomes
    "Ignoring AI's hidden buyers raises costs", not "… raises deal". The first clause wins when it
    already fits and asserts something; otherwise subject + verb group + object are compressed to
    their noun heads, in that order, until the clause fits. A thesis with no verb at all (a label)
    is given one: its noun group plus "matters".

    Args:
        thesis: Stage 1's thesis.

    Returns:
        The clause, sentence-cased; '' only for an empty thesis.
    """
    words = _clause_words(thesis)
    if not words:
        return ""
    if _fits(words) and any(_is_verb_word(w) for w in words):
        return _sentence_cased(words)
    index = next((i for i, w in enumerate(words) if i > 0 and _is_verb_word(w)), None)
    if index is None:
        # A label: take the verb from the WHOLE thesis if it has one, else assert it matters.
        whole = [w.strip(".!?\"“”") for w in (thesis or "").split() if w.strip(".!?\"“”")]
        index = next((i for i, w in enumerate(whole) if i > 0 and _is_verb_word(w)), None)
        if index is None:
            subject = _noun_phrase(words)[:_HOOK_MAX_WORDS - 1]
            return _sentence_cased([*subject, "matters"])
        words = whole
    end = index + 1
    while end < len(words) and (_is_verb_word(words[end]) or words[end].lower() in ("not",
                                                                                   "never")):
        end += 1
    if end < len(words) and (words[end - 1].lower() in ("not", "never")
                             or words[end - 1].lower() in _ASSERTING):
        end += 1  # "does not guarantee": the main verb after an auxiliary belongs to the group
    subject, verbs, rest = words[:index], words[index:end], words[end:]
    obj = _noun_phrase(rest)
    while obj and obj[-1].lower() in _TRAILING:
        obj = obj[:-1]
    subject_np = _noun_phrase(subject) or subject
    lean = [w for w in obj if w.lower() not in _FILLER]
    head = [next((w for w in reversed(lean) if w.lower() not in _FILLER), "")] if lean else []
    head = [w for w in head if w]
    candidates = (
        [*subject, *verbs, *obj],
        [*subject, *verbs, *lean],
        [*subject, *verbs, *head],
        [*subject_np, *verbs, *lean],
        [*subject_np, *verbs, *head],
        [*subject_np[-2:], *verbs, *head],
        [*subject_np[-1:], *verbs, *head],
        [*subject_np[-1:], *verbs[-1:], *head],
    )
    for candidate in candidates:
        if _fits(candidate):
            return _sentence_cased(candidate)
    return _sentence_cased([*subject_np[-1:], verbs[-1]])


def restore_number_casing(hook: str, source: str) -> str:
    """Every number token in ``hook`` spelled exactly as the source spells it ("$30K", not "$30k").

    Args:
        hook: The hook.
        source: The analysed text.

    Returns:
        The hook with each number restored; unchanged where the source has no match.
    """
    def restore(match: "re.Match[str]") -> str:
        token = match.group(0)
        found = re.search(rf"(?<![\w.$€£]){re.escape(token)}(?![\d])", source or "",
                          re.IGNORECASE)
        return found.group(0) if found else token

    return _HOOK_NUMBER.sub(restore, hook or "")


def fit_hook(hook: str, thesis: str) -> str:
    """``hook`` when it is at most ``_HOOK_MAX_WORDS`` words, else ``clause_hook(thesis)``.

    The backstop where a CALLER's concept reaches the brief without Stage 1's regeneration:
    a long hook is replaced by a complete clause, never cut mid-phrase (round 15).

    Args:
        hook: The concept's hook.
        thesis: The concept's thesis.

    Returns:
        A hook of at most ``_HOOK_MAX_WORDS`` words.
    """
    if len((hook or "").split()) <= _HOOK_MAX_WORDS:
        return hook
    return clause_hook(thesis) or clause_hook(hook)


def _valid_hook(hook: str, title: Optional[str], topic_text: str = "",
                source: Optional[str] = None) -> str:
    """The hook, in sentence case, if ``hook_rejection`` passes it; else ''."""
    hook = (hook or "").strip().strip("\"'“”‘’").strip()
    if not hook or hook_rejection(hook, title, topic_text, source):
        return ""
    # Sentence case: the first letter up (round 7: "audit stops AI waste"); acronyms untouched.
    return hook[:1].upper() + hook[1:] if hook[:1].islower() else hook


_CLAUSE_BREAK = re.compile(r"[,;:—–]| - |\b(?:because|so|while|which|but|as|after|when)\b",
                           re.IGNORECASE)
_TRAILING = frozenset({"and", "or", "but", "of", "to", "with", "for", "a", "an", "the", "by", "in",
                       "on", "at", "its", "their", "your", "our", "is", "are", "was", "were"})


def _sentence_cased(words: Sequence[str]) -> str:
    out = [w if (len(w) >= 2 and w.isupper()) or i == 0 else w.lower()
           if w[:1].isupper() and w[1:].islower() else w for i, w in enumerate(words)]
    text = " ".join(out)
    return text[:1].upper() + text[1:]


def derive_hook(thesis: str, source: str, title: Optional[str] = None,
                facts: Sequence[str] = (), anchors: Sequence[str] = ()) -> str:
    """A deterministic hook for a composited surface when Stage 1 gave no valid one: never ''.

    First the thesis trimmed at its first clause boundary to at most ``_HOOK_MAX_WORDS`` words
    (a leading article and trailing function words dropped); if that fails the hook rules, a
    grounded number from the facts plus the first anchor's noun ("$30K billing review"); and if
    that fails too, the trimmed thesis anyway — a composited image always carries a headline.

    Args:
        thesis: Stage 1's thesis.
        source: The analysed text.
        title: The piece's title.
        facts: Stage 1's grounded ``specific_entities``.
        anchors: Stage 1's grounded visual anchors.

    Returns:
        The hook, in sentence case.
    """
    # Round 15: a COMPLETE clause of at most six words — never a mid-phrase truncation, and
    # never a label (``clause_hook`` gives a verbless thesis a verb).
    trimmed = clause_hook(thesis)
    if trimmed and number_claim_mismatch(trimmed, source):
        # Round 14: never a false pairing, even in the last resort — drop the number.
        trimmed = clause_hook(" ".join(w for w in (thesis or "").split()
                                       if not _HOOK_NUMBER.fullmatch(w.strip(".,;:"))))
    topic = " ".join((thesis or "", *facts, *anchors))
    valid = _valid_hook(trimmed, title, topic, source)
    if valid:
        return valid
    lowered = (source or "").lower()
    noun = next((" ".join(a.split()[-2:]) for a in anchors if a and not any(
        ch.isdigit() for ch in a)), "")
    for fact in (*facts, thesis or ""):
        for stat in stat_numbers(fact or ""):
            if stat.lower() in lowered and noun:
                candidate = _valid_hook(f"{_source_casing(stat, source)} {noun}", title, topic,
                                        source)
                if candidate:
                    return candidate
    return trimmed or "What changed here"


# Round 9: "Verification is much cheaper" — cheaper than what? A comparative needs its reference.
_COMPARATIVES = frozenset({
    "cheaper", "faster", "slower", "better", "worse", "more", "less", "fewer", "higher", "lower",
    "bigger", "smaller", "greater", "larger", "longer", "shorter", "easier", "harder", "safer",
    "riskier", "quicker", "smarter", "stronger", "weaker", "richer", "poorer", "cleaner",
    "clearer", "simpler", "leaner", "deeper", "wider", "tighter", "sooner", "earlier", "newer",
    "older", "costlier", "pricier", "busier", "louder", "quieter", "sharper", "slimmer",
})


def vague_comparative(hook: str) -> Optional[str]:
    """The comparative in ``hook`` that has no reference — no "than" and no number — or None.

    Args:
        hook: The hook.

    Returns:
        The comparative word, or None when the hook has none or anchors it ("45% less
        engagement", "cheaper than a hire").
    """
    tokens = re.findall(r"[a-z]+", (hook or "").lower())
    if "than" in tokens or any(ch.isdigit() for ch in hook or ""):
        return None
    return next((t for t in tokens if t in _COMPARATIVES), None)


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


def _prop_reason(anchor: str) -> str:
    """Why an anchor is a prop the brief would refuse to draw (round 8), or ''."""
    from cqc_lem.utilities.ai.image_brief import prop_failure, tech_hardware

    hardware = tech_hardware(anchor)
    return prop_failure(anchor) or (f"names tech hardware ({hardware!r})" if hardware else "")


_CALENDAR_WORDS = re.compile(
    r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|January|February|March|"
    r"April|May|June|July|August|September|October|November|December)\b")


def _ground_setting(raw: Any, source: str, facts: Sequence[str]) -> str:
    """Stage 1's setting, validated like an anchor (round 12), or '' when it is unusable.

    The cast rotation once supplied the setting, and "warehouse floor" turned up on four of nine
    gauntlet items whatever the topic. The setting now comes from the article: refused when it
    names a name, a number, a prop, tech hardware or a stock symbol, or when nothing in it appears
    in the source.

    Args:
        raw: The analyst's ``setting``.
        source: The analysed text.
        facts: The concept's ``specific_entities``.

    Returns:
        The setting, first letter lowercased, or ''.
    """
    from cqc_lem.utilities.ai.image_brief import cliche_hit

    setting = _clean(raw, 100)
    if not setting:
        return ""
    # A weekday or month is a time, not a name: "late on a Tuesday" is the story's own scene.
    plain = _CALENDAR_WORDS.sub(lambda m: m.group(0).lower(), setting)
    reason = (anchor_rejection(plain, facts, source) or _prop_reason(setting)
              or ("a stock symbol" if cliche_hit(setting) else ""))
    lowered = source.lower()
    grounded = any(re.search(rf"\b{re.escape(_stem(t))}", lowered)
                   for t in _content_tokens(setting))
    if reason or not grounded:
        log_debug("Image concept setting dropped", setting=setting, action_type="image_concept",
                  reason=reason or "nothing in it appears in the source")
        return ""
    return setting[:1].lower() + setting[1:] if setting[:1].isupper() else setting


def _ground_anchors(raw: Any, source: str, facts: Sequence[str]) -> tuple[str, ...]:
    anchors: list[str] = []
    lowered_source = source.lower()
    for item in (raw if isinstance(raw, list) else []):
        anchor = _clean(item, 80)
        if not anchor or anchor.lower() in (a.lower() for a in anchors):
            continue
        reason = anchor_rejection(anchor, facts, source) or _prop_reason(anchor)
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
    people = _filter_ideas([payload.get("people_idea")], entities)
    hint = _clean(payload.get("archetype"), 40).lower()
    raw_nouns = payload.get("idea_nouns") if isinstance(payload.get("idea_nouns"), list) else []
    nouns = tuple(n for n in (_clean(x, 40) for x in raw_nouns) if n)[:20]
    return ImageConcept(
        thesis=thesis,
        audience=_clean(payload.get("audience"), 120),
        specific_entities=entities,
        emotional_beat=_clean(payload.get("emotional_beat"), 120),
        valence=(_clean(payload.get("valence"), 20).lower()
                 if _clean(payload.get("valence"), 20).lower() in VALENCES else "mixed"),
        hook_shape=hook_shape_of(hook) if hook else "",
        hook_options=options or None,
        kicker=valid_kicker(payload.get("kicker"), source) or derive_kicker(source, title),
        setting=_ground_setting(payload.get("setting"), source, entities),
        hook_phrase=hook,
        treatment=treatment,
        treatment_rationale=_clean(payload.get("treatment_rationale")),
        weak=len(anchors) < WEAK_ENTITY_FLOOR,
        visual_anchors=anchors,
        visual_ideas=_filter_ideas(payload.get("visual_ideas"), entities),
        archetype_hint=hint if hint in ARCHETYPES else "",
        graphic=_validated_graphic(payload.get("graphic_facts"), source),
        human_moment=payload.get("human_moment") is True,
        people_idea=people[0] if people else "",
        idea_nouns=nouns,
    )


def _validated_graphic(raw: Any, source: str) -> Optional[dict[str, Any]]:
    """Stage 1's graphic facts with every unverifiable item dropped; None when none were given."""
    if not isinstance(raw, dict):
        return None
    from cqc_lem.utilities.ai.image_graphics import validate_graphic_facts

    return validate_graphic_facts(raw, source)


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
    # The idea is for the AI render — the END of the archetype chain, whatever draws first.
    renders = (concept.archetype_ranking or (concept.archetype,))[-1]
    if renders == ARCHETYPE_PEOPLE and concept.people_idea:
        # A human moment: the analyst's one people-led idea IS the image.
        return dataclasses.replace(concept, chosen_idea=concept.people_idea,
                                   rejected_ideas=tuple(concept.visual_ideas),
                                   idea_pick_reason="human moment — the people-led idea")
    editorial = renders == ARCHETYPE_EDITORIAL
    ideas = list(concept.visual_ideas)
    if editorial:
        # An editorial concept shows no person: a people-led idea is not a candidate.
        ideas = [i for i in ideas if not is_people_led(i)]
    if not ideas:
        return concept
    if editorial:
        default = next((i for i in ideas if not is_desk_still_life(i)), ideas[0])
        winner, reason = default, "ranking unavailable — first object idea"
    else:
        default = next((i for i in ideas if is_people_led(i)), ideas[0])
        winner, reason = default, "ranking unavailable — first people-led idea"
    if len(ideas) > 1:
        from cqc_lem.utilities.ai.ai_helper import _loads_json_object
        from cqc_lem.utilities.ai.client import client

        try:
            response = client.chat.completions.create(
                model="lem-simple",
                messages=[{"role": "user", "content": (
                    _EDITORIAL_PICK_PROMPT if editorial else _IDEA_PICK_PROMPT).format(
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
                    # Deterministic backstop: an object on a desk is a still life, not an idea.
                    better = [i for i in ranked if (not is_desk_still_life(i) if editorial
                                                    else is_people_led(i))]
                    if better:
                        winner = better[0]
                        reason = ("object-on-a-desk idea demoted for the next object idea"
                                  if editorial else
                                  "object-on-a-desk idea demoted for the best people-led one")
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
# Round 8: only SPLIT layouts rotate — the type panel and the scene never overlap, so a headline
# can never sit on a face or a torso (every overlay layout did). The compositor still supports the
# overlay layouts for a caller that asks.
COVER_LAYOUTS = ("split_left", "split_right")
POST_LAYOUTS = ("split_top", "split_bottom")
SURFACE_LAYOUTS = {"newsletter": COVER_LAYOUTS, "post_image": POST_LAYOUTS}
# Each dimension rotates INDEPENDENTLY, so no pairing (of age, gender, ethnicity or setting) is
# ever correlated with another — or with the role, which always comes from the piece's anchors.
CAST_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "gender": ("woman", "man"),
    "age": ("20s", "30s", "40s", "50s", "60s"),
    "ethnicity": ("Black", "East Asian", "South Asian", "Hispanic", "white", "Middle Eastern",
                  "Southeast Asian"),
}
# Round 12 (#2241): the SETTING is no longer rotated — it comes from the article (``setting``).
# Round 13: every scene was a medium shot of 2-4 people at a table in a beige office. The FRAMING
# rotates least-recently-used from the receipts, like the cast; the place never does.
SHOTS = (
    "a close-up single portrait, one face filling the frame",
    "an over-the-shoulder two-shot, the camera behind one person looking at the other",
    "a walking-and-talking two-shot, two people mid-stride side by side",
    "one person standing at a window, half-turned toward the camera",
    "one person presenting to a small group, seen from behind the group",
)
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


_SETTING_LEAD = re.compile(r"^(?:in|on|at|during|behind|inside|outside|by|near)\b",
                           re.IGNORECASE)
_ARTICLE_LEAD = re.compile(r"^(?:a|an|the|her|his|their|our|its|\w+'s)\b", re.IGNORECASE)


def _setting_clause(setting: str) -> str:
    """", at a client's open-plan office" — or '' with no setting."""
    setting = (setting or "").strip()
    if not setting:
        return ""
    if _SETTING_LEAD.match(setting):
        return f", {setting}"
    if not _ARTICLE_LEAD.match(setting):
        setting = f"{'an' if setting[:1].lower() in 'aeiou' else 'a'} {setting}"
    return f", at {setting}"


def cast_phrase(cast: Optional[dict[str, str]]) -> str:
    """The brief's cast line: "a {ethnicity} {gender} in her/his {age}, a {role}, at {setting}".

    Args:
        cast: The rotated cast dict, or None.

    Returns:
        The phrase, or '' without a cast.
    """
    if not cast:
        return ""
    pronoun = "her" if cast.get("gender") == "woman" else "his"
    role = cast["role"]
    article = "an" if role[:1].lower() in "aeiou" else "a"
    return (f"a {cast['ethnicity']} {cast['gender']} in {pronoun} {cast['age']}, {article} "
            f"{role}{_setting_clause(cast.get('setting', ''))}")


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
                           recent_casts: Optional[Sequence[dict]] = None,
                           recent_shots: Optional[Sequence[str]] = None) -> ImageConcept:
    """Rotate the composition and (for a people_scene) the person, least-recently-used.

    Args:
        concept: Stage 1's concept.
        surface: Only surfaces with a layout table get a layout; covers, posts and video frames
            get a cast.
        recent_layouts: The last ``ROTATION_WINDOW`` layouts on this surface, most recent first.
        recent_casts: The last ``ROTATION_WINDOW`` cast dicts, most recent first.
        recent_shots: The last ``ROTATION_WINDOW`` shots (``SHOTS``), most recent first.

    Returns:
        The concept with ``layout``, ``cast`` and ``shot`` set (unchanged when not applicable).
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
        cast["setting"] = concept.setting
    shot = concept.shot
    if surface in _CAST_SURFACES and concept.treatment == TREATMENT_PEOPLE and not shot:
        shot = _least_recent(SHOTS, list(recent_shots or [])[:ROTATION_WINDOW], seed + "shot")
    return dataclasses.replace(concept, layout=layout, cast=cast, shot=shot)


# Round 10 (#2241): the surfaces ``image_compose`` typesets a headline onto — each MUST have one.
_HOOKED_SURFACES = frozenset({"newsletter", "post_image"})
_HOOK_RETRY = """Every hook you offered for this piece was rejected:
{reasons}

The piece's thesis: {thesis}
Write ONE new hook: 2-6 words, sentence case, no exclamation mark, never an order; every word
from the piece; a number must name its subject; a comparative ("cheaper", "more", "faster")
needs "than ..." or a number. Respond with ONLY a JSON object: {{"hook_phrase": "..."}}"""


def _offered_hooks(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    raw = payload.get("hook_candidates")
    offered = list(raw.values()) if isinstance(raw, dict) else []
    offered += [payload.get("hook_phrase")] + list(payload.get("hook_alternatives") or [])
    return [_clean(h, 80) for h in offered if _clean(h, 80)]


_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


# Round 13 (#2241): the lead-number rule grabbed "4.5" from "Claude Opus 4.5". A headline number
# is a STAT — a percentage, a currency amount, a multiplier, or a number followed by a unit or a
# count noun — never a product/model version next to a capitalised name, and never a year.
_COUNT_NOUNS = (
    "posts|hours|days|weeks|months|years|minutes|customers|clients|users|people|teams|leads|"
    "seats|tools|calls|replies|deals|buyers|employees|staff|percent|times|invoices|meetings|"
    "comments|followers|views|signups|sales|orders|projects|steps|errors|tickets|subscribers")
_STAT = re.compile(
    r"[$€£]\d[\d,.]*\s?(?:[KkMmBb]n?\b|million\b|billion\b|thousand\b)?"
    r"|\b\d[\d,.]*\s?%"
    r"|\b\d+(?:\.\d+)?[xX×](?!\w)"
    rf"|\b\d[\d,.]*(?=\s+(?:more\s+|fewer\s+|less\s+|new\s+)?(?:{_COUNT_NOUNS})\b)",
    re.IGNORECASE)
_YEAR = re.compile(r"^(?:19|20)\d\d$")
_VERSIONED = re.compile(r"\b[A-Z][A-Za-z0-9]*[\s\-]?$")


def stat_numbers(text: str) -> list[str]:
    """The STATS ``text`` states, in order, as written.

    Args:
        text: Any text.

    Returns:
        Each stat ("$30K", "60%", "3x", "45" in "45 posts"); never a version next to a
        capitalised name ("Opus 4.5", "GPT-5.2") or a bare year.
    """
    found = []
    for match in _STAT.finditer(text or ""):
        stat = match.group(0).strip().rstrip(".,")
        before = (text or "")[:match.start()]
        if not re.search(r"[$€£%]", stat) and _VERSIONED.search(before):
            continue  # "Opus 4.5", "GPT-5.2": a version next to a name, not a stat
        if _YEAR.match(stat):
            continue
        found.append(stat)
    return found


def _source_casing(stat: str, source: str) -> str:
    """``stat`` as the SOURCE writes it ("$30K", never "$30k")."""
    match = re.search(re.escape(stat), source or "", re.IGNORECASE)
    return match.group(0) if match else stat


def thesis_number(concept: ImageConcept, source: str, title: Optional[str] = None) -> str:
    """The grounded number tied to the THESIS, or '' (round 12).

    A number from the thesis or the facts that appears verbatim in the source AND in the title,
    the first two sentences of the body, or the same source sentence as the thesis claim (one
    sharing at least half of the thesis's content words).

    Args:
        concept: Stage 1's concept.
        source: The analysed text (title first, as Stage 1 reads it).
        title: The piece's title.

    Returns:
        The number as the source writes it ("$30K", "45%"), or ''.
    """
    lowered = (source or "").lower()
    body = source or ""
    if title and body.startswith(title):
        body = body[len(title):]
    sentences = [s for s in _SENTENCE.split(body.strip()) if s.strip()]
    thesis_tokens = set(_content_tokens(concept.thesis))
    claim_sentences = [s for s in sentences if thesis_tokens and len(
        thesis_tokens & set(_content_tokens(s))) * 2 >= len(thesis_tokens)]
    tied_text = " ".join([title or "", *sentences[:2], *claim_sentences]).lower()
    # Round 14: among tied stats, the one whose OWN sentence best matches the thesis wins — ed18's
    # title stat (53.7%, a share of posts) must not beat the engagement gap (45%).
    claim = {t for t in thesis_tokens if t not in _HOOK_FREE_WORDS and len(t) >= 4}
    best, best_score = "", -1
    for text in (concept.thesis, *concept.specific_entities):
        for number in stat_numbers(text):
            if number.lower() not in lowered or number.lower() not in tied_text:
                continue
            holders = [s for s in source_sentences(source) if number.lower() in s.lower()]
            score = max((len(claim & set(_content_tokens(s))) for s in holders), default=0)
            if score > best_score:
                best, best_score = _source_casing(number, source), score
    return best


def lead_with_number(concept: ImageConcept, source: str,
                     title: Optional[str] = None, build: bool = True) -> ImageConcept:
    """Force a ``number_claim`` hook carrying the thesis's lead number, when there is one.

    Round 12: the critic's best cover led with its stat ("45%"); four others left theirs out. A
    valid hook already carrying the number wins (Stage 1's hook first, then its options); else
    the number plus the first anchor's noun, when that passes the hook rules. Shape rotation
    stays in charge only when the thesis has no such number.

    Args:
        concept: The concept after shape rotation.
        source: The analysed text.
        title: The piece's title.
        build: Whether to build "number + noun" when no offered hook carries the number;
            ``_enforce_stat`` asks the analyst first and builds only as the last resort.

    Returns:
        The concept, its hook led by the number when one could be made.
    """
    number = thesis_number(concept, source, title)
    if not number:
        return concept
    candidates = [concept.hook_phrase, *(concept.hook_options or {}).values()]
    hook = next((h for h in candidates if h and number.lower() in h.lower()), "")
    if not hook and build:
        topic = " ".join((concept.thesis, *concept.specific_entities, *concept.visual_anchors))
        noun = next((" ".join(a.split()[-2:]) for a in concept.visual_anchors
                     if a and not any(ch.isdigit() for ch in a)), "")
        hook = _valid_hook(f"{number} {noun}", title, topic, source) if noun else ""
    if not hook:
        return concept
    options = dict(concept.hook_options or {})
    options["number_claim"] = hook
    return dataclasses.replace(concept, hook_phrase=hook, hook_shape="number_claim",
                               hook_options=options)


_HOOK_THESIS_CHECK = """A headline is set beside an image for a LinkedIn piece.

The piece's main claim: {thesis}
The source sentence the headline cites: "{cited}"
The headline: "{hook}"

1. Does the headline ASSERT the piece's main claim — not a caveat, a side point or a neutral
   "X vs Y" that takes no side?
2. Is this headline grammatical English, at most 6 words, and literally true according to the
   source sentence it cites (every number measures what the headline says it measures)?
3. What is the headline's own valence: positive (a saving, a win, relief), negative (a risk, a
   loss, a mistake) or mixed?
Respond with ONLY a JSON object:
{{"asserts_thesis": true|false, "grammatical_and_true": true|false,
 "valence": "positive|negative|mixed", "reason": "<one sentence>"}}"""


def _enforce_stat(concept: ImageConcept, payload: Any, source: str, title: Optional[str],
                  surface: str, user_id: Optional[int]) -> ImageConcept:
    """The thesis stat MUST survive into the hook, verbatim (round 13).

    Post 102 argued "cut AI spend by 60%" and shipped "Routing saves most spend". After
    ``lead_with_number``, a hook still missing the stat is regenerated ONCE with that reason; if
    the analyst still drops it, the hook is the stat plus the first anchor's noun.
    """
    number = thesis_number(concept, source, title)
    concept = lead_with_number(concept, source, title, build=False)
    if not number or number in concept.hook_phrase:
        return concept
    reason = f'the hook must contain the stat "{number}" verbatim — it is the piece\'s lead number'
    retried = _ensure_hook(dataclasses.replace(concept, hook_phrase=""), payload, source, title,
                           surface, user_id, extra_reasons=(reason,))
    if number in retried.hook_phrase:
        return dataclasses.replace(retried, hook_shape="number_claim")
    built = lead_with_number(concept, source, title)
    if number in built.hook_phrase:
        return built
    noun = next((" ".join(a.split()[-2:]) for a in concept.visual_anchors
                 if a and not any(ch.isdigit() for ch in a)), "") or "saved"
    hook = f"{number} {noun}"  # the stat survives even when no rule-passing hook could carry it
    if number_claim_mismatch(hook, source):
        return concept  # round 14: never a false pairing — the number is dropped instead
    return dataclasses.replace(concept, hook_phrase=hook, hook_shape="number_claim",
                               hook_options={**(concept.hook_options or {}), "number_claim": hook})


def check_hook_against_thesis(hook: str, thesis: str, cited: str = "") -> tuple[bool, str, str]:
    """ONE ``lem-simple`` call judging the hook: claim, grammar, length, truth and valence.

    Does the hook assert the thesis, is it grammatical, at most 6 words and literally true
    against the sentence it cites (round 14), and what is its own valence?

    Fails OPEN — an unreachable or unreadable judge passes the hook with no valence; the
    deterministic fidelity rule (``number_claim_mismatch``) already stands behind it.

    Args:
        hook: The headline.
        thesis: Stage 1's thesis.
        cited: The source sentence the hook cites (``cited_sentence``).

    Returns:
        ``(asserts_thesis, valence or '', reason)``.
    """
    from cqc_lem.utilities.ai.ai_helper import _loads_json_object
    from cqc_lem.utilities.ai.client import client

    try:
        response = client.chat.completions.create(
            model="lem-simple",
            messages=[{"role": "user", "content": _HOOK_THESIS_CHECK.format(
                thesis=thesis, hook=hook, cited=cited or "(none)")}],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=_CONCEPT_MAX_TOKENS,
            reasoning_effort=REASONING_EFFORT,
        )
        answer = _loads_json_object(response.choices[0].message.content or "") or {}
    except Exception as e:
        log_debug("Hook-vs-thesis check unavailable — passing the hook", error=str(e),
                  action_type="image_concept")
        return True, "", "judge unavailable"
    valence = str(answer.get("valence") or "").lower()
    ok = (answer.get("asserts_thesis") is not False
          and answer.get("grammatical_and_true") is not False)
    return ok, valence if valence in VALENCES else "", str(answer.get("reason") or "")[:200]


def _hook_asserts_thesis(concept: ImageConcept, payload: Any, source: str,
                         title: Optional[str], surface: str,
                         user_id: Optional[int]) -> ImageConcept:
    """Regenerate a hook that states a caveat or a neutral "vs" (round 12); set its valence.

    Post 140 headlined its closing caveat ("Cheap-first hurts precision") while arguing FOR
    routing. ONE regeneration with the judge's reason, then the face's valence follows the hook
    that ships — 140's subject grinned at "hurts".
    """
    if not concept.hook_phrase:
        return concept
    ok, valence, reason = check_hook_against_thesis(
        concept.hook_phrase, concept.thesis, cited_sentence(concept.hook_phrase, source))
    if not ok:
        rejected = (f'"{concept.hook_phrase}": it does not assert the main claim, or is not '
                    f'grammatical and literally true ({reason})')
        concept = _ensure_hook(dataclasses.replace(concept, hook_phrase=""), payload, source,
                               title, surface, user_id, extra_reasons=(rejected,))
        concept = _enforce_stat(concept, payload, source, title, surface, user_id)
        ok, again, _ = check_hook_against_thesis(
            concept.hook_phrase, concept.thesis, cited_sentence(concept.hook_phrase, source))
        valence = again or valence
        if not ok:
            # Round 14: a second failure takes the DETERMINISTIC hook — the thesis, trimmed.
            hook = derive_hook(concept.thesis, source, title, concept.specific_entities,
                               concept.visual_anchors)
            concept = dataclasses.replace(concept, hook_phrase=hook, hook_shape=hook_shape_of(hook),
                                          hook_options={hook_shape_of(hook): hook})
    return dataclasses.replace(concept, valence=valence) if valence else concept


def _final_hook(concept: ImageConcept, payload: Any, source: str, title: Optional[str],
                surface: str, user_id: Optional[int]) -> ImageConcept:
    """The last step of hook selection: <=6 words HARD, never truncated; source number casing.

    Round 15: round 14's cap cut "Ignoring AI's hidden buyers raises deal costs" to "… raises
    deal". A long hook is REGENERATED with the reason; still long, it takes ``clause_hook`` — a
    complete clause by construction. Then every number is spelled as the source spells it.
    """
    if len(concept.hook_phrase.split()) > _HOOK_MAX_WORDS:
        reason = (f'"{concept.hook_phrase}": {len(concept.hook_phrase.split())} words — the '
                  f"hook is at most {_HOOK_MAX_WORDS} words, a complete claim")
        concept = _ensure_hook(dataclasses.replace(concept, hook_phrase=""), payload, source,
                               title, surface, user_id, extra_reasons=(reason,))
        if len(concept.hook_phrase.split()) > _HOOK_MAX_WORDS:
            hook = clause_hook(concept.thesis)
            concept = dataclasses.replace(concept, hook_phrase=hook,
                                          hook_shape=hook_shape_of(hook))
    hook = concept.hook_phrase
    if hook and not _HOOK_NUMBER.search(hook) and not asserts_something(hook):
        # The claim-not-label rule holds on EVERY path, the fallbacks included (round 15).
        hook = clause_hook(concept.thesis) or hook
        concept = dataclasses.replace(concept, hook_phrase=hook, hook_shape=hook_shape_of(hook))
    restored = restore_number_casing(concept.hook_phrase, source)
    if restored != concept.hook_phrase:
        concept = dataclasses.replace(concept, hook_phrase=restored)
    return concept


def _ensure_hook(concept: ImageConcept, payload: Any, source: str, title: Optional[str],
                 surface: str, user_id: Optional[int],
                 extra_reasons: Sequence[str] = ()) -> ImageConcept:
    """A composited surface's concept with a hook: Stage 1 retried ONCE, else ``derive_hook``.

    The retry carries every rejection reason back to the analyst (round 10: two posts shipped
    with no headline after the comparative rule refused their only hook). It fails OPEN to the
    deterministic hook — a composite never goes out bare.
    """
    from cqc_lem.utilities.ai.ai_helper import _loads_json_object
    from cqc_lem.utilities.ai.client import client

    topic = " ".join((concept.thesis, *concept.specific_entities, *concept.visual_anchors))
    offered = _offered_hooks(payload)
    reasons = "\n".join([f'- "{h}": {hook_rejection(h, title, topic, source) or "unusable"}'
                          for h in offered if not extra_reasons]
                         + [f"- {r}" for r in extra_reasons]) or "- (no hook offered)"
    hook = ""
    try:
        response = client.chat.completions.create(
            model="lem-medium",
            messages=[{"role": "system", "content": _SYSTEM_PROMPT},
                      {"role": "user", "content": (
                          f"<title>{title or ''}</title>\n<content>{source}</content>\n\n"
                          + _HOOK_RETRY.format(reasons=reasons, thesis=concept.thesis))}],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=_CONCEPT_MAX_TOKENS,
            reasoning_effort=REASONING_EFFORT,
        )
        retry = _loads_json_object(response.choices[0].message.content or "") or {}
        hook = _valid_hook(_clean(retry.get("hook_phrase"), 80), title, topic, source)
    except Exception as e:
        log_debug("Hook retry unavailable — deriving one", error=str(e), user_id=user_id,
                  surface=surface, action_type="image_concept")
    source_of_hook = "retry"
    if not hook:
        hook = derive_hook(concept.thesis, source, title, concept.specific_entities,
                           concept.visual_anchors)
        source_of_hook = "derived"
    log_debug("Composited surface hook recovered", user_id=user_id, surface=surface,
              action_type="image_concept", hook_source=source_of_hook)
    shape = hook_shape_of(hook)
    return dataclasses.replace(concept, hook_phrase=hook, hook_shape=shape,
                               hook_options={shape: hook})


def archetype_candidates(concept: ImageConcept) -> list[str]:
    """Which archetypes this concept can take (§6.3 step 2).

    A code-drawn archetype only when its data VALIDATED (``image_graphics.available_archetypes``)
    and there is a headline to set beside it; a people scene only for a human moment; an
    editorial concept always.

    Args:
        concept: Stage 1's concept.

    Returns:
        The candidates, in ``ARCHETYPES`` order.
    """
    from cqc_lem.utilities.ai.image_graphics import available_archetypes

    candidates = list(available_archetypes(concept.graphic)) if concept.hook_phrase else []
    if concept.human_moment:
        candidates.append(ARCHETYPE_PEOPLE)
    candidates.append(ARCHETYPE_EDITORIAL)
    return candidates


def rank_archetypes(candidates: Sequence[str], hint: str = "",
                    recent: Optional[Sequence[str]] = None) -> list[tuple[str, float]]:
    """Score and order the candidates. Deterministic: same inputs, same order.

    Score = ``ARCHETYPE_BASE_SCORES`` + ``ARCHETYPE_TIEBREAK`` for the analyst's pick −
    ``ARCHETYPE_ROTATION_PENALTY`` when the archetype is one of the last ``ARCHETYPE_WINDOW``.
    Equal scores go to code-drawn first (cheaper, no AI look, no garbled text), then
    ``ARCHETYPES`` order.

    Args:
        candidates: From ``archetype_candidates``.
        hint: The analyst's pick.
        recent: The archetypes of the most recent images, most recent first.

    Returns:
        ``(archetype, score)`` pairs, best first.
    """
    window = [r for r in (recent or []) if r][:ARCHETYPE_WINDOW]
    scored = []
    for archetype in dict.fromkeys(candidates):
        score = ARCHETYPE_BASE_SCORES.get(archetype, 0.0)
        if archetype == hint:
            score += ARCHETYPE_TIEBREAK
        if archetype in window:
            score -= ARCHETYPE_ROTATION_PENALTY
        scored.append((archetype, score))
    return sorted(scored, key=lambda pair: (-pair[1], pair[0] not in CODE_DRAWN_ARCHETYPES,
                                            ARCHETYPES.index(pair[0])))


def select_archetype(concept: ImageConcept, surface: str,
                     recent_archetypes: Optional[Sequence[str]] = None) -> ImageConcept:
    """Choose the archetype and its fallback chain, and point the AI treatment at the chain's end.

    Off the composited surfaces the concept is returned unchanged. The chain runs best-first and
    stops at the first AI archetype, which is what renders when every code-drawn one ahead of it
    fails (no data fits, a figure does not trace, the judge refuses it). That AI archetype sets
    the treatment: ``editorial_concept`` for everything but a human moment — the people photo is
    demoted from the default.

    Args:
        concept: Stage 1's concept.
        surface: The surface.
        recent_archetypes: Archetypes of this author's most recent images, most recent first.

    Returns:
        The concept with ``archetype``, ``archetype_ranking``, ``archetype_rationale`` and the
        ``treatment`` set.
    """
    if surface not in ARCHETYPE_SURFACES:
        return concept
    ranked = rank_archetypes(archetype_candidates(concept), concept.archetype_hint,
                             recent_archetypes)
    chain: list[str] = []
    for archetype, _score in ranked:
        chain.append(archetype)
        if archetype in AI_ARCHETYPES:
            break
    ai = chain[-1]
    treatment = TREATMENT_PEOPLE if ai == ARCHETYPE_PEOPLE else TREATMENT_EDITORIAL
    rationale = ", ".join(f"{a}={score:g}" for a, score in ranked)
    log_debug("Image archetype selected", action_type="image_concept", archetype=chain[0],
              chain=",".join(chain), scores=rationale)
    return dataclasses.replace(concept, archetype=chain[0], archetype_ranking=tuple(chain),
                               archetype_rationale=rationale, treatment=treatment,
                               cast=concept.cast if treatment == TREATMENT_PEOPLE else None)


def ai_archetype_only(concept: Optional[ImageConcept]) -> Optional[ImageConcept]:
    """The concept with its code-drawn archetypes stripped — for a caller that must render.

    The admin variant tool compares RENDERS; a code-drawn graphic would make every variant the
    same picture.

    Args:
        concept: A concept, or None.

    Returns:
        The concept with ``archetype`` set to its AI fallback (unchanged when already AI).
    """
    if concept is None or concept.archetype not in CODE_DRAWN_ARCHETYPES:
        return concept
    chain = tuple(a for a in concept.archetype_ranking if a in AI_ARCHETYPES) or (
        ARCHETYPE_EDITORIAL,)
    return dataclasses.replace(concept, archetype=chain[0], archetype_ranking=chain)


def assign_art_style(concept: ImageConcept,
                     recent_styles: Optional[Sequence[str]] = None) -> ImageConcept:
    """Rotate the editorial art style least-recently-used, when the chain ends in one.

    Args:
        concept: The concept after ``select_archetype``.
        recent_styles: Art styles of the most recent images, most recent first.

    Returns:
        The concept with ``art_style`` set (unchanged when no editorial concept can render).
    """
    if concept.treatment != TREATMENT_EDITORIAL or concept.art_style:
        return concept
    style = _least_recent(tuple(ART_STYLES), list(recent_styles or [])[:ROTATION_WINDOW],
                          concept.thesis + "style")
    return dataclasses.replace(concept, art_style=style)


@llm_step("image_concept")
def analyze_content_for_image(text: str, *, title: Optional[str] = None,
                              surface: str = "post_image",
                              user_id: Optional[int] = None,
                              recent_treatments: Optional[Sequence[str]] = None,
                              recent_layouts: Optional[Sequence[str]] = None,
                              recent_casts: Optional[Sequence[dict]] = None,
                              recent_hook_shapes: Optional[Sequence[str]] = None,
                              recent_shots: Optional[Sequence[str]] = None,
                              recent_archetypes: Optional[Sequence[str]] = None,
                              recent_art_styles: Optional[Sequence[str]] = None,
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
        recent_shots: Shots (framings) of the most recent images, most recent first.
        recent_archetypes: Archetypes of the most recent images, most recent first — the last
            ``ARCHETYPE_WINDOW`` are penalised (``select_archetype``).
        recent_art_styles: Art styles of the most recent editorial concepts, most recent first.

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
        for _ in range(2):
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
    if surface in _HOOKED_SURFACES and not concept.hook_phrase:
        concept = _ensure_hook(concept, payload, source, title, surface, user_id)
    # The archetype is chosen BEFORE the idea pick: the pick is for the chain's AI end.
    concept = select_archetype(enforce_graphic_cap(concept, recent, surface), surface,
                               recent_archetypes)
    concept = assign_art_style(pick_visual_idea(concept, surface), recent_art_styles)
    concept = rotate_hook_shape(concept, recent_hook_shapes)
    if surface in _HOOKED_SURFACES:
        # Round 12: the lead number outranks shape rotation, and the hook must assert the thesis.
        concept = _enforce_stat(concept, payload, source, title, surface, user_id)
        concept = _hook_asserts_thesis(concept, payload, source, title, surface, user_id)
        concept = _final_hook(concept, payload, source, title, surface, user_id)
        # A code-drawn graphic needs the FINAL headline; re-rank against it (deterministic).
        concept = select_archetype(concept, surface, recent_archetypes)
    return assign_layout_and_cast(concept, surface, recent_layouts, recent_casts, recent_shots)
