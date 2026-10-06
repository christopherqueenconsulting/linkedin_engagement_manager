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
_HOOK_MIN_WORDS, _HOOK_MAX_WORDS, _HOOK_MAX_CHARS = 2, 5, 32
# lem-medium is a reasoning model: thinking tokens bill against this budget before any JSON.
_CONCEPT_MAX_TOKENS = 2500
# At most ONE editorial_graphic in any GRAPHIC_WINDOW consecutive covers (gauntlet round 1: every
# edition chose a graphic, so the series had no variety at all).
GRAPHIC_WINDOW = 3

# Generic acronyms that are not anyone's name. Everything else that is capitalised mid-phrase, or
# carries a digit, is a name or a number — a FACT, never something to draw.
GENERIC_ACRONYMS = frozenset({"AI", "B2B", "B2C", "CEO", "CFO", "CTO", "COO", "CMO", "HR", "IT",
                              "SaaS"})

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
{{"thesis": "<the piece's actual argument, one sentence>",
 "audience": "<who it is written for, a few words>",
 "specific_entities": ["<3-6 facts the text names: companies, products, reports, numbers, roles>"],
 "visual_anchors": ["<3-5 DEPICTABLE things from the piece: roles, places, physical artifacts, \
actions or situations>"],
 "emotional_beat": "<the feeling the reader should get, a few words>",
 "hook_phrase": "<2-5 words, at most 32 characters>",
 "hook_alternatives": ["<two more hooks, same rules>"],
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
("Stop…", "Start…", "Don't…"), never a restatement of the title, never a full sentence. The hook \
carries the THESIS; the image carries emotion and specificity.

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
    "hook_phrase is required.")

# Round 3 (#2241): abstract theses ("AI hallucinates", "hidden buyers") cannot be carried by a
# literal scene, so Stage 1 proposes several ideas and a cheap judge picks one (the Idea2Img
# pattern: generate candidates, rank, build from the winner).
_VISUAL_IDEAS = 3
_PEOPLE_WORDS = re.compile(
    r"\b(?:face|faces|her|his|their|she|he|person|people|man|woman|men|women|team|colleague|"
    r"founder|owner|lead|manager|editor|analyst|marketer|consultant|client|buyer|officer|"
    r"executive|expression|smile|frown|wince|laugh|eyebrow)s?\b", re.IGNORECASE)
_IDEA_PICK_MAX_TOKENS = 1500
_IDEA_PICK_PROMPT = """You are picking ONE image idea for a LinkedIn {surface}.

The piece argues: {thesis}
Its headline (hook): {hook}

Candidate ideas:
{ideas}

Rank them on: specificity to THIS article, surprise, legibility as a 400x225 thumbnail, and \
distance from stock clichés. Respond with ONLY a JSON object:
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


def _valid_hook(hook: str, title: Optional[str]) -> str:
    """The hook if it is a usable curiosity gap; else ''.

    2-5 words, at most ``_HOOK_MAX_CHARS``, no exclamation mark, no imperative opener, and not
    the title again.
    """
    hook = hook.strip().strip("\"'“”‘’").strip()
    words = hook.split()
    if not (_HOOK_MIN_WORDS <= len(words) <= _HOOK_MAX_WORDS) or len(hook) > _HOOK_MAX_CHARS:
        return ""
    if "!" in hook or words[0].lower().strip(",.:;") in _IMPERATIVE_OPENERS:
        return ""  # an order or a shout is an ad, not a curiosity gap
    hook_tokens = set(_content_tokens(hook))
    title_tokens = set(_content_tokens(title or ""))
    if hook_tokens and title_tokens and hook_tokens <= title_tokens:
        return ""  # a restatement of the title closes no gap
    return hook


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
    from cqc_lem.utilities.ai.image_brief import cliche_hit

    hit = cliche_hit(idea)
    if hit:
        return f"stock symbol {hit!r}"
    names = name_tokens(idea)
    if names:
        return f"names or numbers {names}"
    lowered = {t.lower() for t in re.findall(r"[A-Za-z0-9]+", idea)}
    for fact in facts:
        if is_fact_only(fact):
            fact_names = {t.lower() for t in name_tokens(fact)} | (
                {fact.split()[0].lower()} if fact[:1].isupper() else set())
            if lowered & (fact_names - {t.lower() for t in GENERIC_ACRONYMS}):
                return f"it names the fact {fact!r}"
    return ""


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


def _first_valid_hook(payload: dict[str, Any], title: Optional[str]) -> str:
    candidates = [payload.get("hook_phrase")] + list(payload.get("hook_alternatives") or [])
    for candidate in candidates:
        hook = _valid_hook(_clean(candidate, 80), title)
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
    thesis = _clean(payload.get("thesis"))
    if not thesis:
        return None
    entities = _ground_entities(payload.get("specific_entities"), source)
    anchors = _ground_anchors(payload.get("visual_anchors"), source, entities)
    # The first hook that passes the rules, so a shouted primary hook does not cost a cover its
    # headline when a usable alternative was offered (round 3: every cover carries one).
    hook = _first_valid_hook(payload, title)
    treatment = _resolve_treatment(_clean(payload.get("treatment"), 40).lower(), anchors, hook)
    return ImageConcept(
        thesis=thesis,
        audience=_clean(payload.get("audience"), 120),
        specific_entities=entities,
        emotional_beat=_clean(payload.get("emotional_beat"), 120),
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
            )
            verdict = _loads_json_object(response.choices[0].message.content or "") or {}
            ranking = [int(n) for n in (verdict.get("ranking") or [])
                       if isinstance(n, (int, float, str)) and str(n).strip().isdigit()]
            ranked = [ideas[n - 1] for n in ranking if 1 <= n <= len(ideas)]
            if ranked:
                winner = ranked[0]
                reason = _clean(verdict.get("reason"), 200) or "ranked first"
        except Exception as e:
            log_debug("Visual idea ranking unavailable — taking the default", error=str(e),
                      action_type="image_concept")
    else:
        reason = "only one idea survived the filters"
    return dataclasses.replace(concept, chosen_idea=winner,
                               rejected_ideas=tuple(i for i in ideas if i != winner),
                               idea_pick_reason=reason)


@llm_step("image_concept")
def analyze_content_for_image(text: str, *, title: Optional[str] = None,
                              surface: str = "post_image",
                              user_id: Optional[int] = None,
                              recent_treatments: Optional[Sequence[str]] = None,
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
    guidance = (_COVER_GUIDANCE + "\n" if surface == "newsletter" else "") + (
        f"Recent images by this author used, most recent first: {', '.join(recent)}. Prefer a "
        f"different treatment than {recent[0]} when the piece allows.\n" if recent else "")
    try:
        response = client.chat.completions.create(
            model="lem-medium",
            messages=[{"role": "system", "content": _SYSTEM_PROMPT},
                      {"role": "user", "content": (
                          f"Surface: {surface}\n{guidance}"
                          f"<title>{title or ''}</title>\n<content>{source}</content>")}],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=_CONCEPT_MAX_TOKENS,
        )
        raw = response.choices[0].message.content or ""
        payload = _loads_json_object(raw)
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
    return pick_visual_idea(enforce_graphic_cap(concept, recent, surface), surface)
