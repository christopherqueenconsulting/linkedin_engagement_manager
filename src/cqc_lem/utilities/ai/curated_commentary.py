"""The writer and the chart-fact reader for curated posts (``docs/curated-sources.md``).

A curated post is OUR commentary on someone else's content, so two rules from the content core
carry the weight here, and neither is new:

- **The voice is the author's.** The prompt is assembled from the same shared core every post uses
  — ``voice_reference``, ``alignment_directive`` (which carries the 70/20/10 mix directive),
  ``post_writing_directive``, ``style_directive``, ``hashtag_directive`` — and the draft runs the
  same slop lint and bounded repair (``ai_helper.lint_repaired``).
- **The facts are the source's.** Every number the commentary states must appear in the source
  snapshot (``content_framework.fact_grounding_report`` with the snapshot as the only anchors). A
  draft that still invents one after one steered retry is refused, never shipped.

The commentary must also NAME the source in its own words and carry a ``Source:`` credit line;
``curated_sources.with_credit`` appends the line deterministically, so the credit never depends on
the model remembering it.

Re-charted figures are read from the SOURCE text and validated by ``image_graphics`` exactly as a
post image's figures are: each drawn string is the sentence's own substring, or it is not drawn.
"""

import json
import re
from typing import Any, Optional

from cqc_lem.utilities.ai import content_framework as _framework
from cqc_lem.utilities.ai.client import client
from cqc_lem.utilities.ai.content_alignment import (
    alignment_directive,
    style_directive,
    voice_reference,
)
from cqc_lem.utilities.curated_sources import (
    PLATFORM_GOV_DATA,
    TREATMENT_LINK,
    TREATMENT_RECHART,
    TREATMENT_RESHARE,
    chart_source_line,
    has_credit,
    with_credit,
)
from cqc_lem.utilities.logger import log_info, log_warning
from cqc_lem.utilities.observability import FEATURE_CONTENT, llm_pipeline, llm_step

# A reshare whose commentary is a one-liner is the "instant repost" failure mode the research warns
# about (LinkedIn suppresses it), so the commentary has a floor — before the credit line.
COMMENTARY_MIN_CHARS = 280
EXCERPT_PROMPT_MAX = 4000

_TREATMENT_BRIEF = {
    TREATMENT_RESHARE: ("LinkedIn will embed the original post directly under your commentary, "
                        "with its author's name and face. Write the commentary that sits above it."),
    TREATMENT_RECHART: ("Your post carries a chart we redrew from the source's own figures. Write "
                        "the commentary that explains why those figures matter to the reader."),
    TREATMENT_LINK: ("Your post carries a link card to the article. Write the commentary that makes "
                     "a reader want to open it, and say why it is worth their time."),
}


def _source_block(source: dict) -> str:
    excerpt = (source.get("excerpt") or "")[:EXCERPT_PROMPT_MAX]
    return (f"Author: {source.get('author') or 'unknown'}\n"
            f"Publisher: {source.get('publisher') or 'unknown'}\n"
            f"Title: {source.get('title') or ''}\n"
            f"URL: {source.get('url') or ''}\n"
            f"<source_text>{excerpt}</source_text>")


def commentary_messages(source: dict, treatment: str, voice: str, prefs: Optional[dict] = None,
                        content_mix: Optional[str] = None, extra: str = "") -> list:
    """The system + user messages for one curated commentary draft.

    Args:
        source: The curated-source row (the snapshot the commentary is about).
        treatment: One of the curated treatments.
        voice: The author's voice reference (``voice_reference``).
        prefs: The author's engagement preferences.
        content_mix: The slot's 70/20/10 class (``value`` or ``authority``).
        extra: A retry steer (slop or fact repair), appended last.

    Returns:
        The chat messages.
    """
    who = source.get("author") or source.get("publisher") or "the source"
    system = (
        "You are writing a LinkedIn post AS the author described below, commenting on someone "
        "else's content. You are NOT its author and must never imply you are.\n"
        f"{_TREATMENT_BRIEF.get(treatment, '')}\n\n"
        "Rules:\n"
        f"- Name the source in your own words early in the post: '{who} found…', "
        f"'{who} reports…', '{who} argues…'. Never state their point as your own claim.\n"
        "- Bring a real take: agree and extend with something the author knows from their own "
        "work, push back on one point, or translate it into what it means for a small-business "
        "owner. A post that only summarizes the source is a failure.\n"
        "- Every number you state must appear in the source text, exactly as written. Never "
        "estimate, round or invent a figure.\n"
        "- Quote at most one short phrase from the source, in quotation marks.\n"
        "- No politics of any kind.\n"
        "- Do not add a 'Source:' line or a URL; both are added for you.\n"
        f"\n### Author voice:\n{voice}\n"
    )
    system += alignment_directive(prefs, "", content_mix)
    system += _framework.post_writing_directive()
    system += style_directive(prefs, "post")
    system += "\n" + _framework.hashtag_directive(prefs)
    user = f"The content you are commenting on:\n{_source_block(source)}\n{extra}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _complete(messages: list, model: str = "lem-medium", json_mode: bool = False) -> str:
    kwargs: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0.6}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    response = client.chat.completions.create(**kwargs)
    return (response.choices[0].message.content or "").strip()


def finish_post_text(text: Optional[str]) -> str:
    """The same deterministic finish a generated text post gets: no markdown, no wall of text.

    #2241 showcase C: curated drafts shipped ``**Define a research question**`` — LinkedIn renders
    no markdown, and the little-text escaper then printed the asterisks literally. Text posts are
    cleaned by ``sanitize_for_linkedin`` (bold, italics, headers, rules, code, links) and reflowed
    by ``shape_for_dwell``; a curated draft now goes through exactly those two, so it follows the
    text post's rules rather than a parallel set. The LENGTH budget is already shared: both prompts
    carry ``post_writing_directive`` (1300-2000 characters).

    Args:
        text: The model's draft.

    Returns:
        The plain-text draft (``""`` for an empty one).
    """
    from cqc_lem.utilities.linkedin_formatter import sanitize_for_linkedin

    return _framework.shape_for_dwell(sanitize_for_linkedin(text or "") or "") or ""


def source_anchors(source: dict) -> list:
    """The only text a curated commentary's numbers may come from: the source's own snapshot."""
    return [t for t in ((source or {}).get("title"), (source or {}).get("excerpt")) if t]


@llm_pipeline("curated_commentary", feature=FEATURE_CONTENT)
def generate_curated_commentary(user_id: int, source: dict, treatment: str, profile=None,
                                profile_synthesis: Optional[str] = None,
                                prefs: Optional[dict] = None,
                                content_mix: Optional[str] = None) -> Optional[str]:
    """Write the commentary for one curated post, credit line included. None when refused.

    Args:
        user_id: The author.
        source: The curated-source row.
        treatment: One of the curated treatments.
        profile: The author's LinkedIn profile (voice fallback when there is no synthesis).
        profile_synthesis: The author's cached voice synthesis.
        prefs: The author's engagement preferences.
        content_mix: The slot's 70/20/10 class.

    Returns:
        The commentary with its ``Source:`` line, or None when the draft invents a number after one
        retry, is too thin to carry a take, or the model returned nothing.
    """
    from cqc_lem.utilities.ai.ai_helper import lint_repaired

    if profile is None and not profile_synthesis:
        voice = "A practical small-business AI and automation consultant."
    else:
        voice = voice_reference(profile, profile_synthesis)

    def draft(extra: str = "") -> str:
        return finish_post_text(_complete(commentary_messages(source, treatment, voice, prefs,
                                                              content_mix, extra)))

    text = draft()
    text = lint_repaired(text, "post", draft, prefs=prefs, user_id=user_id,
                         task_name="curated_commentary")
    anchors = source_anchors(source)
    report = _framework.fact_grounding_report(text, anchors)
    if text and not report["passes"]:
        text = draft(_framework.fact_retry_directive(report))
        report = _framework.fact_grounding_report(text, anchors)
    if not text:
        return None
    if not report["passes"]:
        log_warning("Curated commentary still states numbers the source does not — not drafted",
                    user_id=user_id, numbers=report["unverified_values"],
                    task_name="curated_commentary")
        return None
    if report["placeholders"]:
        log_info("Curated commentary left placeholders — refused, the source is the only fact base",
                 user_id=user_id, task_name="curated_commentary")
        return None
    if len(text) < COMMENTARY_MIN_CHARS:
        log_info("Curated commentary too thin to carry a take — not drafted", user_id=user_id,
                 chars=len(text), task_name="curated_commentary")
        return None
    out = with_credit(text, source, treatment)
    return out if has_credit(out, source) else None


# --- Re-chart facts ------------------------------------------------------------------------------

_CHART_PROMPT = """Read the source text and return JSON with the figures a data chart could draw.
Copy every value and every source_sentence EXACTLY from the text. A number the text does not
state, a baseline it does not give, or a total you computed is FORBIDDEN: leave the field null.
Every word of a label comes from its own sentence.

{"thesis_stat": {"label": "...", "value": "...", "unit": "$ | % | x | a unit word | ''",
                 "source_sentence": "..."} | null,
 "comparison": {"measure": "...", "items": [2-6 items shaped like thesis_stat, same measure and
                unit], "highlight": <index>, "annotation": "<at most 8 words from the text>"}
               | null}"""

_GOV_SENTENCE = re.compile(
    r"^(?P<label>[A-Za-z][^.\n]{3,80}?) (?:was|were) (?P<cur>\$)?(?P<value>\d[\d,]*(?:\.\d+)?)"
    r"(?P<unit> percent|%)? in (?P<period>[A-Z][a-z]+ \d{4})\.$")


def gov_graphic_facts(source: dict) -> dict:
    """The raw ``graphic_facts`` a government-data source states, read deterministically.

    The gov-data collector writes its excerpt as one sentence per reading in a fixed shape
    (``"<label> was <value> in <Month YYYY>."``), so the figure is read off the sentence itself —
    no model in between. The result is still validated by ``validate_graphic_facts``.
    """
    for line in (source.get("excerpt") or "").splitlines():
        match = _GOV_SENTENCE.match(line.strip())
        if match:
            unit = "$" if match.group("cur") else ("%" if match.group("unit") else "")
            return {"thesis_stat": {"label": match.group("label"), "value": match.group("value"),
                                    "unit": unit, "source_sentence": line.strip()}}
    return {}


@llm_step("curated_chart_facts")
def extract_chart_facts(source: dict) -> dict:
    """The raw figures a re-chart could draw, read FROM THE SOURCE. ``{}`` when none or on error."""
    if source.get("platform") == PLATFORM_GOV_DATA:
        return gov_graphic_facts(source)
    text = (source.get("excerpt") or "")[:EXCERPT_PROMPT_MAX]
    if not text.strip():
        return {}
    try:
        raw = _complete([{"role": "system", "content": _CHART_PROMPT},
                         {"role": "user", "content": f"<source_text>{text}</source_text>"}],
                        model="lem-simple", json_mode=True)
        data = json.loads(raw or "{}")
    except Exception as e:
        log_warning("Could not read chart figures from a curated source", exc=e,
                    task_name="curated_commentary")
        return {}
    return data if isinstance(data, dict) else {}


def validated_chart(source: dict, raw: Any) -> Optional[dict]:
    """``raw`` figures validated against the SOURCE snapshot; None when nothing is drawable.

    Every figure must be its sentence's own substring and every sentence must be in the source
    (``image_graphics.validate_graphic_facts``). The on-card source line is the credit, never a
    name the model wrote.
    """
    from cqc_lem.utilities.ai.image_graphics import available_archetypes, validate_graphic_facts

    snapshot = "\n".join(source_anchors(source))
    graphic = validate_graphic_facts(raw if isinstance(raw, dict) else {}, snapshot)
    graphic["source_line"] = chart_source_line(source)
    drawable = [a for a in available_archetypes(graphic) if a in ("stat_card", "highlight_chart")]
    return graphic if drawable else None
