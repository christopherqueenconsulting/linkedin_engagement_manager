"""Stage 1 of the image engine: read the WHOLE piece and decide what its image should show.

Before this stage the brief author saw a title, a few candidate objects and one mechanism sentence,
so every newsletter cover became the same metaphor still-life — a valve, a gauge, a gear — whatever
the article was about (issue #2241). This module reads up to ``_MAX_SOURCE_CHARS`` of the actual
text and returns an ``ImageConcept``: the thesis, who it is for, the concrete entities the text
itself names, and the TREATMENT the image should take. ``image_brief`` writes the render prompt
from that, and ``image_gen``'s vision gate grades the render against it.

Grounding is enforced, not requested: an entity the text never mentions is dropped, because an
invented entity is how an article-specific brief turns back into a generic one.

Fails soft: any error returns ``None`` and every caller has a deterministic path without it.
"""

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Optional

from cqc_lem.utilities.logger import log_debug
from cqc_lem.utilities.observability import llm_step

TREATMENT_PEOPLE = "people_scene"
TREATMENT_GRAPHIC = "editorial_graphic"
TREATMENT_CONCRETE = "concrete_scene"
TREATMENT_METAPHOR = "metaphor_last_resort"
TREATMENTS = (TREATMENT_PEOPLE, TREATMENT_GRAPHIC, TREATMENT_CONCRETE, TREATMENT_METAPHOR)

# Enough of a long newsletter edition to reach its payoff, which usually sits well past the hook.
_MAX_SOURCE_CHARS = 12000
_MIN_ENTITIES, _MAX_ENTITIES = 3, 6
# Below this many grounded entities the concept is WEAK: still usable for treatment and thesis,
# but the entity-coverage checks downstream stand down rather than demand what is not there.
WEAK_ENTITY_FLOOR = 2
_HOOK_MIN_WORDS, _HOOK_MAX_WORDS, _HOOK_MAX_CHARS = 2, 5, 32
# lem-medium is a reasoning model: thinking tokens bill against this budget before any JSON.
_CONCEPT_MAX_TOKENS = 2500

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
 "specific_entities": ["<3-6 concrete nouns, actors, settings or numbers>"],
 "emotional_beat": "<the feeling the reader should get, a few words>",
 "hook_phrase": "<2-5 words, at most 32 characters>",
 "treatment": "{'|'.join(TREATMENTS)}",
 "treatment_rationale": "<one sentence>"}}

Rules for specific_entities: copy them FROM THE TEXT — people, roles, places, objects, documents, \
tools, numbers it actually names. Never invent one, never generalise one into a category, never \
add a symbol the text does not mention.

Rules for hook_phrase: a curiosity gap a reader would want closed — never a restatement of the \
title, never a full sentence.

Choose the treatment:
- {TREATMENT_PEOPLE}: the piece is about people, teams, clients, hiring or a decision someone \
makes. The image is a candid documentary photograph of those people in that situation.
- {TREATMENT_GRAPHIC}: the core is a number, a contrast or a sharp claim that reads best as a \
short hook. The image is a designed editorial graphic carrying the hook_phrase.
- {TREATMENT_CONCRETE}: the piece describes a specific tangible situation — a pile of invoices, \
a dashboard on a real desk, a warehouse shelf. A screen is allowed when it IS the subject.
- {TREATMENT_METAPHOR}: ONLY when nothing concrete exists in the text. Even then, an uncommon \
metaphor specific to this piece — never a stock symbol such as a lightbulb, gears, a puzzle \
piece, pipes or valves, a rocket, a chess board, a compass or a handshake."""


@dataclass(frozen=True)
class ImageConcept:
    """What one piece of content is about, reduced to what its image must show.

    Attributes:
        thesis: The piece's actual argument in one sentence.
        audience: Who the piece is written for.
        specific_entities: Concrete nouns the TEXT names — every one is grounded in the source.
        emotional_beat: The feeling the image should carry.
        hook_phrase: A 2-5 word curiosity gap; only an ``editorial_graphic`` ever renders it.
        treatment: One of ``TREATMENTS``.
        treatment_rationale: Why that treatment, as the analyst put it.
        weak: True when fewer than ``WEAK_ENTITY_FLOOR`` entities survived grounding, so the
            entity-coverage checks downstream stand down.
    """

    thesis: str
    audience: str
    specific_entities: tuple[str, ...]
    emotional_beat: str
    hook_phrase: str
    treatment: str
    treatment_rationale: str
    weak: bool = False

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
    depicts them.

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


def _clean(value: Any, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def _valid_hook(hook: str, title: Optional[str]) -> str:
    """The hook if it is 2-5 words, fits ``_HOOK_MAX_CHARS`` and is not the title again; else ''."""
    hook = hook.strip().strip("\"'“”‘’").strip()
    words = hook.split()
    if not (_HOOK_MIN_WORDS <= len(words) <= _HOOK_MAX_WORDS) or len(hook) > _HOOK_MAX_CHARS:
        return ""
    hook_tokens = set(_content_tokens(hook))
    title_tokens = set(_content_tokens(title or ""))
    if hook_tokens and title_tokens and hook_tokens <= title_tokens:
        return ""  # a restatement of the title closes no gap
    return hook


def _ground_entities(raw: Any, source: str) -> tuple[str, ...]:
    entities: list[str] = []
    for item in (raw if isinstance(raw, list) else []):
        entity = _clean(item, 80)
        if not entity or entity.lower() in (e.lower() for e in entities):
            continue
        if entity_mentioned(entity, source):
            entities.append(entity)
        else:
            log_debug("Image concept entity dropped — not in the source text", entity=entity,
                      action_type="image_concept")
    return tuple(entities[:_MAX_ENTITIES])


def _resolve_treatment(raw: str, entities: tuple[str, ...], hook: str) -> str:
    treatment = raw if raw in TREATMENTS else TREATMENT_CONCRETE
    if treatment == TREATMENT_GRAPHIC and not hook:
        # A graphic with no usable hook is a graphic with nothing to say.
        treatment = TREATMENT_CONCRETE
    if treatment == TREATMENT_METAPHOR and len(entities) >= _MIN_ENTITIES:
        # "Only when nothing concrete exists" — three grounded entities say something does.
        treatment = TREATMENT_CONCRETE
    return treatment


def parse_concept(payload: Optional[dict[str, Any]], source: str,
                  title: Optional[str] = None) -> Optional[ImageConcept]:
    """Validate a raw Stage 1 reply into an ``ImageConcept``, or None when it is unusable.

    Args:
        payload: The parsed JSON object the analyst returned.
        source: The text the analyst read; entities are grounded against it.
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
    hook = _valid_hook(_clean(payload.get("hook_phrase"), 80), title)
    treatment = _resolve_treatment(_clean(payload.get("treatment"), 40).lower(), entities, hook)
    return ImageConcept(
        thesis=thesis,
        audience=_clean(payload.get("audience"), 120),
        specific_entities=entities,
        emotional_beat=_clean(payload.get("emotional_beat"), 120),
        hook_phrase=hook,
        treatment=treatment,
        treatment_rationale=_clean(payload.get("treatment_rationale")),
        weak=len(entities) < WEAK_ENTITY_FLOOR,
    )


@llm_step("image_concept")
def analyze_content_for_image(text: str, *, title: Optional[str] = None,
                              surface: str = "post_image",
                              user_id: Optional[int] = None) -> Optional[ImageConcept]:
    """Read the full content and decide what its image must show. Never raises.

    One ``lem-medium`` JSON call over up to ``_MAX_SOURCE_CHARS`` of ``text``.

    Args:
        text: The FULL content — a whole edition body, not its hook.
        title: The piece's title, when it has one.
        surface: The surface the image is for; named to the analyst as context only.
        user_id: The author, for log context.

    Returns:
        The concept, or None when the call fails or returns nothing usable — callers keep a
        deterministic path for exactly that case.
    """
    from cqc_lem.utilities.ai.ai_helper import _loads_json_object
    from cqc_lem.utilities.ai.client import client

    source = "\n\n".join(p for p in (title, text) if p and p.strip())[:_MAX_SOURCE_CHARS]
    if not source.strip():
        return None
    try:
        response = client.chat.completions.create(
            model="lem-medium",
            messages=[{"role": "system", "content": _SYSTEM_PROMPT},
                      {"role": "user", "content": (
                          f"Surface: {surface}\n"
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
    return concept
