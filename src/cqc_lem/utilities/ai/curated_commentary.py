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

Showcase round 7: all four curated drafts ran one template ("For a small-business owner…", a
3-step checklist, a closing "Which…?"), named product features the source never stated, and said
"In practice" about a model nobody here had run. So the STRUCTURE and its close rotate
(``CURATED_STRUCTURES``, least-recently-used against the author's recent curated posts); a named
product or feature the source text does not contain is a fact-gate failure like an invented number
(``unsourced_names``); and a first-hand testing claim is re-attributed to the source unless a
story-bank fact backs it (``first_hand_claims``).

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
from cqc_lem.utilities.logger import log_debug, log_info, log_warning
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


# --- Structure and close rotation (showcase round 7) ----------------------------------------------

STRUCTURE_TAKEAWAY = "takeaway_contrarian"
STRUCTURE_EXAMPLE = "single_example"
STRUCTURE_PROS_CONS = "pros_cons"
STRUCTURE_TEST_FIRST = "test_first"
STRUCTURE_CHECKLIST = "checklist"  # the retired template, read off legacy posts only
# key -> (shape directive, close directive). No shape is a numbered checklist, and no close is a
# "Which…?" question.
CURATED_STRUCTURES: dict = {
    STRUCTURE_TAKEAWAY: (
        "ONE takeaway from the source in plain words, then ONE point where you disagree, add a "
        "caveat, or name what it leaves out. No list.",
        "Close on your own position in one sentence. No question."),
    STRUCTURE_EXAMPLE: (
        "ONE concrete example of what this changes for your reader, walked through start to "
        "finish: who, what they do today, what they would do with it. No list.",
        "Close by telling the reader what to keep or save from this, in one sentence."),
    STRUCTURE_PROS_CONS: (
        "Weigh it: what it does well and what it costs or risks, a sentence or two each, as "
        "prose. Not a numbered checklist.",
        "Close with an open question about the trade-off that does NOT start with 'Which'."),
    STRUCTURE_TEST_FIRST: (
        "Say what you would test first and what result would change your mind. Attribute every "
        "capability to the source; you have not tested it.",
        "Close by asking readers what they would measure, in a question that does NOT start "
        "with 'Which'."),
}
CURATED_STRUCTURE_ORDER = (STRUCTURE_TAKEAWAY, STRUCTURE_EXAMPLE, STRUCTURE_PROS_CONS,
                           STRUCTURE_TEST_FIRST)
# How many recent curated posts the rotation reads.
CURATED_STRUCTURE_WINDOW = 4
_LIST_LINE_RE = re.compile(r"^\s*(?:[-•*▪✓✔]|\d{1,2}[.)])\s+\S", re.MULTILINE)
_STRUCTURE_SIGNS = (
    (STRUCTURE_PROS_CONS, re.compile(r"\b(?:upside|downside|pros?|cons?|trade-?offs?|the catch)\b",
                                     re.IGNORECASE)),
    (STRUCTURE_TEST_FIRST, re.compile(r"\b(?:test(?:ed)? first|i'?d test|i would test|"
                                      r"i'?d measure|change my mind)\b", re.IGNORECASE)),
    (STRUCTURE_EXAMPLE, re.compile(r"\b(?:for example|picture (?:a|an|your)|imagine|say you|"
                                   r"take (?:a|an) )", re.IGNORECASE)),
    (STRUCTURE_TAKEAWAY, re.compile(r"\b(?:takeaway|where i disagree|i'?d push back|"
                                    r"the caveat|what it leaves out)\b", re.IGNORECASE)),
)
# The template opener every round-7 draft used.
_TEMPLATE_OPENER_RE = re.compile(
    r"^\s*(?:for|if you(?:'re| are| run))\s+(?:a\s+|the\s+)?small[- ]business(?:es)?"
    r"(?:\s+owners?)?[^,.\n]{0,40},\s*", re.IGNORECASE)
_WHICH_CLOSE_RE = re.compile(r"^\W*which\b[^\n]*\?\s*$", re.IGNORECASE)


def curated_structure_of(text: Optional[str]) -> Optional[str]:
    """The structure a curated post was written in, read off its text (None when unreadable)."""
    body = text or ""
    if len(_LIST_LINE_RE.findall(body)) >= 3:
        return STRUCTURE_CHECKLIST
    return next((key for key, rx in _STRUCTURE_SIGNS if rx.search(body)), None)


def select_curated_structure(recent_curated_texts: Optional[list], seed: int = 0) -> str:
    """The structure THIS curated post is written in: least recently used of the author's last few.

    Args:
        recent_curated_texts: The author's recent curated posts, most recent first.
        seed: A stable per-post integer (the source id) breaking ties.

    Returns:
        One of ``CURATED_STRUCTURE_ORDER``.
    """
    recent = [curated_structure_of(t) for t in list(recent_curated_texts or [])
              [:CURATED_STRUCTURE_WINDOW]]
    order = list(CURATED_STRUCTURE_ORDER)
    offset = int(seed or 0) % len(order)
    order = order[offset:] + order[:offset]

    def rank(key: str) -> int:
        return recent.index(key) if key in recent else len(recent) + 1

    best = max(rank(k) for k in order)
    return next(k for k in order if rank(k) == best)


def structure_directive(structure: Optional[str]) -> str:
    """The writer's shape-and-close instruction for one structure; '' for an unknown key."""
    shape, close = CURATED_STRUCTURES.get(structure or "", ("", ""))
    if not shape:
        return ""
    return (f"\n### Structure for THIS post:\n- {shape}\n- {close}\n"
            "- Do NOT open with 'For a small-business owner' or any other audience label; open on "
            "the source's point.\n"
            "- Never end on a 'Which…?' question.\n")


def strip_template_opener(text: Optional[str]) -> Optional[str]:
    """``text`` without a leading "For a small-business owner, …" audience label."""
    if not text:
        return text
    match = _TEMPLATE_OPENER_RE.match(text)
    if not match:
        return text
    rest = text[match.end():]
    return rest[:1].upper() + rest[1:] if rest else text


def strip_which_close(text: Optional[str]) -> Optional[str]:
    """``text`` without a final "Which …?" question paragraph, when the post stands without it."""
    paragraphs = [p for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]
    if len(paragraphs) < 3 or not _WHICH_CLOSE_RE.match(paragraphs[-1].strip().split("\n")[-1]):
        return text
    return "\n\n".join(paragraphs[:-1]).strip()


# --- Facts the source does not state (showcase round 7) -------------------------------------------

# Capitalised words that name nothing a source has to state.
_COMMON_CAPS = frozenset((
    "I", "AI", "A", "An", "The", "This", "That", "These", "Those", "It", "If", "When", "What",
    "Why", "How", "Which", "Who", "My", "Our", "Your", "We", "You", "They", "LinkedIn", "Source",
    "But", "And", "Or", "So", "For", "In", "On", "At", "To", "Start", "Here", "There", "Then",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January",
    "February", "March", "April", "May", "June", "July", "August", "September", "October",
    "November", "December", "CEO", "CFO", "CTO", "SMB", "SMBs", "API", "APIs", "ROI", "KPI",
))
_NAME_RUN_RE = re.compile(r"\b[A-Z][\w.+&'’-]*(?:\s+[A-Z][\w.+&'’-]*)*")
_FIRST_HAND_RES = (
    re.compile(r"\bin practice\b,?\s*", re.IGNORECASE),
    re.compile(r"\bin my (?:own )?(?:testing|tests|trials?)\b,?\s*", re.IGNORECASE),
    re.compile(r"\b(?:when )?i (?:tested|tried|ran|benchmarked|piloted) (?:it|this|the|them)\b",
               re.IGNORECASE),
    re.compile(r"\bi'?ve been (?:using|testing|running) (?:it|this|the|them)\b", re.IGNORECASE),
)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def unsourced_names(text: Optional[str], allowed: Optional[list]) -> list:
    """Named products, features or organisations in the commentary that no allowed text names.

    A capitalised run that does not open its sentence ("Copilot Home", "Power Automate") is a
    claim about the world; the source snapshot (plus the author's own voice brief) is the only
    place it may come from, exactly as with a number.

    Args:
        text: The commentary.
        allowed: The texts a name may come from.

    Returns:
        The unsourced names, in order.
    """
    haystack = " ".join(str(a) for a in (allowed or []) if a).lower()
    found: list = []
    for line in (text or "").splitlines():
        for sentence in _SENTENCE_RE.split(line.strip()):
            for match in _NAME_RUN_RE.finditer(sentence):
                words = [re.sub(r"['’]s$", "", w) for w in match.group(0).split()]
                words = [w for w in words if w not in _COMMON_CAPS]
                opener = match.start() == 0 or sentence[:match.start()].rstrip().endswith(
                    (":", "\"", "“"))
                if opener and len(match.group(0).split()) == 1:
                    continue  # one capitalised word opening a sentence is grammar, not a name
                name = " ".join(words).strip(" .,'’")
                if name and name.lower() not in haystack and name not in found \
                        and not all(w.lower() in haystack for w in words):
                    found.append(name)
    return found


def first_hand_claims(text: Optional[str]) -> list:
    """The phrases where the commentary implies the author tested the thing themselves."""
    return [m.group(0).strip(" ,") for rx in _FIRST_HAND_RES for m in rx.finditer(text or "")]


def first_hand_supported(source: dict, facts: Optional[list]) -> bool:
    """Does a story-bank fact show the author actually used what the source is about?"""
    names = [str((source or {}).get(k) or "").strip().lower() for k in ("publisher", "author")]
    title_words = [w.lower() for w in re.findall(r"[A-Z][\w.+-]{2,}", str((source or {}).get(
        "title") or ""))]
    keys = [n for n in names + title_words if n and n not in ("the", "and")]
    return any(k in str(f).lower() for f in (facts or []) for k in keys)


def attribute_first_hand(text: Optional[str], who: str) -> str:
    """Every first-hand testing claim re-attributed to the source, or its sentence dropped."""
    out = text or ""
    out = re.sub(r"\b(?i:in practice),?\s*(\w)",
                 lambda m: f"According to {who}, {m.group(1).lower()}", out)
    kept = []
    for line in out.split("\n"):
        sentences = [s for s in _SENTENCE_RE.split(line)
                     if not any(rx.search(s) for rx in _FIRST_HAND_RES[1:])]
        kept.append(" ".join(sentences))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def drop_sentences_naming(text: Optional[str], names: list) -> str:
    """``text`` without the sentences that state any of ``names``."""
    kept = []
    for line in (text or "").split("\n"):
        kept.append(" ".join(s for s in _SENTENCE_RE.split(line)
                             if not any(n in s for n in names)))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def source_names_directive(names: list) -> str:
    """The ONE retry's steer after the commentary named things the source does not."""
    return ("\n\nYOUR PREVIOUS DRAFT NAMED PRODUCTS OR FEATURES THE SOURCE TEXT NEVER STATES ("
            + ", ".join(f'"{n}"' for n in names[:6]) + "). Rewrite it naming only what the source "
            "text names, and say nothing about features it does not describe.\n")


def _recent_curated_texts(user_id: int) -> list:
    """The author's recent CURATED posts (they carry a ``Source:`` credit line). Never raises."""
    try:
        from cqc_lem.utilities.db import get_recent_post_texts

        texts = get_recent_post_texts(user_id, limit=20) or []
    except Exception as e:
        log_debug("Recent posts unreadable for the curated structure rotation", error=str(e),
                  user_id=user_id, task_name="curated_commentary")
        return []
    return [t for t in texts if isinstance(t, str) and re.search(r"(?m)^Source:", t)]


def commentary_messages(source: dict, treatment: str, voice: str, prefs: Optional[dict] = None,
                        content_mix: Optional[str] = None, extra: str = "",
                        structure: Optional[str] = None) -> list:
    """The system + user messages for one curated commentary draft.

    Args:
        source: The curated-source row (the snapshot the commentary is about).
        treatment: One of the curated treatments.
        voice: The author's voice reference (``voice_reference``).
        prefs: The author's engagement preferences.
        content_mix: The slot's 70/20/10 class (``value`` or ``authority``).
        extra: A retry steer (slop or fact repair), appended last.
        structure: The rotated ``CURATED_STRUCTURES`` key (showcase round 7).

    Returns:
        The chat messages.
    """
    who = source.get("author") or source.get("publisher") or "the source"
    vendor = _vendor_name(source)
    system = (
        "You are writing a LinkedIn post AS the author described below, commenting on someone "
        "else's content. You are NOT its author and must never imply you are.\n"
        f"{_TREATMENT_BRIEF.get(treatment, '')}\n\n"
        "Rules:\n"
        f"- Name the source in your own words early in the post: '{who} found…', "
        f"'{who} reports…', '{who} argues…'. Never state their point as your own claim.\n"
        f"- Speak in YOUR voice ABOUT the source. Refer to {vendor} and its products in the THIRD "
        f"person — '{vendor}'s models', '{vendor}'s API' — never 'our models', 'our API' or 'we "
        "launched': you do not work there.\n"
        "- Bring a real take: agree and extend with something the author knows from their own "
        "work, push back on one point, or translate it into what it means for a small-business "
        "owner. A post that only summarizes the source is a failure.\n"
        "- Every number you state must appear in the source text, exactly as written. Never "
        "estimate, round or invent a figure.\n"
        "- Name only the products, features and capabilities the source text itself describes.\n"
        "- You have NOT tested this yourself: never write 'In practice', 'I tested' or 'when I "
        "tried it'. Attribute every capability to the source.\n"
        "- Quote at most one short phrase from the source, in quotation marks.\n"
        "- No politics of any kind.\n"
        "- Do not add a 'Source:' line or a URL; both are added for you.\n"
        f"\n### Author voice:\n{voice}\n"
    )
    system += alignment_directive(prefs, "", content_mix)
    system += _framework.post_writing_directive()
    system += style_directive(prefs, "post")
    system += "\n" + _framework.hashtag_directive(prefs)
    system += structure_directive(structure)
    user = f"The content you are commenting on:\n{_source_block(source)}\n{extra}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _vendor_name(source: dict) -> str:
    """Who the commentary must speak ABOUT in the third person: the publisher, else the author."""
    return str((source or {}).get("publisher") or (source or {}).get("author")
               or "the source").strip()


# First-person-plural possessives tied to a vendor's product (showcase round 4): curated_1 shipped
# "OpenAI reports that Jump Trading is using our models", which reads as if the author were OpenAI.
# Vendor-product nouns only, so the author's own "our clients", "our team" or "we built" is never
# touched.
_VENDOR_PRODUCT_NOUNS = (r"models?|apis?|sdks?|platform|products?|chips?|gpus?|researchers?|"
                         r"launch|release|announcement")
_VENDOR_VOICE_RE = re.compile(
    r"\bour\s+(?:(?:own|new|latest|newest)\s+)?(?:" + _VENDOR_PRODUCT_NOUNS + r")\b"
    r"|\bwe(?:'ve| have)?\s+(?:just\s+)?(?:launched|released|announced)\b",
    re.IGNORECASE)


def vendor_voice_hits(text: Optional[str]) -> list:
    """The phrases where the commentary speaks AS the vendor ("our models", "we launched")."""
    return [m.group(0) for m in _VENDOR_VOICE_RE.finditer(text or "")]


def vendor_voice_directive(hits: list, vendor: str) -> str:
    """The ONE rewrite's steer: name the phrases and the third-person form to use instead."""
    listed = ", ".join(f'"{h}"' for h in hits[:5])
    return (f"\n\nYOUR PREVIOUS DRAFT SPOKE AS IF YOU WORKED AT {vendor.upper()} ({listed}). "
            f"Rewrite it in your own voice: refer to {vendor} in the third person ('{vendor}'s "
            f"models', '{vendor} launched'). Keep everything else.\n")


def third_person_vendor(text: str, vendor: str) -> str:
    """The deterministic last word: every vendor-voice phrase re-pointed at the vendor by name."""
    def fix(match: "re.Match") -> str:
        phrase = match.group(0)
        if phrase.lower().startswith("our"):
            return f"{vendor}'s" + phrase[3:]
        rest = re.sub(r"^we(?:'ve| have)?\s+", "", phrase, flags=re.IGNORECASE)
        return f"{vendor} {rest}"

    return _VENDOR_VOICE_RE.sub(fix, text or "")


def _complete(messages: list, model: str = "lem-medium", json_mode: bool = False) -> str:
    kwargs: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0.6}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    response = client.chat.completions.create(**kwargs)
    return (response.choices[0].message.content or "").strip()


def _source_faithful(text: Optional[str], source: dict, voice: str, draft, user_id: int) -> str:
    """The round-7 faithfulness pass: names, first-hand claims, template opener and close.

    A named product or feature the source does not state gets ONE rewrite; what survives it loses
    its sentence. A first-hand testing claim is re-attributed to the source unless a story-bank
    fact shows the author used it. The template opener and a "Which…?" close are cut in code.
    """
    if not text:
        return text or ""
    allowed = source_anchors(source) + [str(source.get("author") or ""),
                                        str(source.get("publisher") or ""), voice or ""]
    names = unsourced_names(text, allowed)
    if names:
        log_info("Curated commentary named things the source does not — rewriting once",
                 user_id=user_id, names=names[:5], task_name="curated_commentary")
        retry = draft(source_names_directive(names))
        if retry and _framework.fact_grounding_report(retry, source_anchors(source))["passes"]:
            text = retry
        names = unsourced_names(text, allowed)
        if names:
            text = drop_sentences_naming(text, names)
    if first_hand_claims(text):
        from cqc_lem.utilities.post_image import story_facts_for

        if not first_hand_supported(source, story_facts_for(user_id)):
            text = attribute_first_hand(text, str(source.get("author") or source.get("publisher")
                                                  or "the source"))
    text = strip_which_close(strip_template_opener(text))
    recent = _recent_curated_texts(user_id)
    text = rotate_curated_opener(text, recent, str(source.get("author") or source.get("publisher")
                                                   or ""))
    text = drop_repeated_pivot(text, recent)
    return finish_post_text(text)


# --- Showcase round 8: substance floor, opener and pivot rotation ---------------------------------

# curated_2 shipped two sentences (282 characters, two over the floor) — a restated headline and
# "opens a practical path for small businesses". A take needs a point, a reason and a consequence:
# this many sentences of commentary, besides `COMMENTARY_MIN_CHARS`.
COMMENTARY_MIN_SENTENCES = 3


def thin_commentary_reason(text: Optional[str]) -> str:
    """Why a curated draft is too thin to publish, or '' when it carries a take.

    Args:
        text: The commentary, before its credit line.

    Returns:
        The reason, or ''.
    """
    body = (text or "").strip()
    sentences = [x for x in _SENTENCE_RE.split(body.replace("\n", " ")) if len(x.split()) >= 3]
    if len(body) < COMMENTARY_MIN_CHARS:
        return f"{len(body)} characters"
    if len(sentences) < COMMENTARY_MIN_SENTENCES:
        return f"{len(sentences)} sentence(s)"
    return ""


# Three of four round-8 drafts opened "<Source> reports that …". The opener rotates: a draft that
# would repeat the last curated post's opening verb is re-set in the next form.
_REPORTS_THAT_RE = re.compile(r"^(?P<who>[A-Z][^\n]{1,80}?)\s+(?P<verb>reports|says|argues|found|"
                              r"finds|writes|notes|announced|announces)\s+that\s+(?P<rest>\S)")
_OPENER_FORMS = ("According to {who}, {rest}", "{who} has a new claim: {rest}",
                 "New from {who}: {rest}")


def opener_form(text: Optional[str]) -> str:
    """How a curated post opens: ``reports_that``, one of ``_OPENER_FORMS``' keys, or ''."""
    first = (text or "").lstrip()
    if _REPORTS_THAT_RE.match(first):
        return "reports_that"
    for n, form in enumerate(_OPENER_FORMS):
        head = form.split("{who}")[0]
        if head and first.startswith(head):
            return f"form_{n}"
        if form.startswith("{who}") and re.match(r"^[A-Z][^\n]{1,80}?" + re.escape(
                form.split("{who}")[1].split("{rest}")[0]), first):
            return f"form_{n}"
    return ""


def rotate_curated_opener(text: Optional[str], recent_curated: Optional[list], who: str) -> str:
    """``text`` with its "<Source> reports that …" opener re-set when the last curated post used it.

    Args:
        text: The commentary.
        recent_curated: The author's recent curated posts, most recent first.
        who: The source's author or publisher.

    Returns:
        The commentary.
    """
    match = _REPORTS_THAT_RE.match((text or "").lstrip())
    if not match or not recent_curated:
        return text or ""
    used = {opener_form(t) for t in list(recent_curated)[:2]}
    if "reports_that" not in used:
        return text or ""
    body = (text or "").lstrip()
    rest = body[match.start("rest"):]
    for n, form in enumerate(_OPENER_FORMS):
        if f"form_{n}" not in used:
            return form.format(who=match.group("who"), rest=rest)
    return text or ""


# "For a small business, …", "From a small-business perspective, …": the pivot three of four drafts
# made. A pivot the last curated post also made is cut to the sentence it introduced.
_SMB_PIVOT_RE = re.compile(
    r"(?m)^(?:for|from)\s+(?:a\s+|the\s+)?small[- ]business(?:es)?(?:\s+owners?)?"
    r"(?:\s+(?:perspective|point\s+of\s+view|lens))?\s*,\s*", re.IGNORECASE)


def drop_repeated_pivot(text: Optional[str], recent_curated: Optional[list]) -> str:
    """``text`` without its small-business pivot label when the last curated post used one."""
    if not text or not _SMB_PIVOT_RE.search(text):
        return text or ""
    if not any(_SMB_PIVOT_RE.search(t or "") for t in list(recent_curated or [])[:2]):
        return text
    out = _SMB_PIVOT_RE.sub("", text)
    return re.sub(r"(?m)^([a-z])", lambda m: m.group(1).upper(), out)


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

    structure = select_curated_structure(_recent_curated_texts(user_id),
                                         int((source or {}).get("id") or 0))

    def draft(extra: str = "") -> str:
        return finish_post_text(_complete(commentary_messages(source, treatment, voice, prefs,
                                                              content_mix, extra, structure)))

    text = draft()
    text = lint_repaired(text, "post", draft, prefs=prefs, user_id=user_id,
                         task_name="curated_commentary")
    vendor = _vendor_name(source)
    hits = vendor_voice_hits(text)
    if text and hits:
        # INFO: one rewrite is the check doing its job; a survivor is re-pointed below.
        log_info("Curated commentary spoke as the vendor — rewriting once", user_id=user_id,
                 phrases=hits[:5], task_name="curated_commentary")
        text = draft(vendor_voice_directive(hits, vendor)) or text
        if vendor_voice_hits(text):
            text = third_person_vendor(text, vendor)
    anchors = source_anchors(source)
    report = _framework.fact_grounding_report(text, anchors)
    if text and not report["passes"]:
        text = draft(_framework.fact_retry_directive(report))
        report = _framework.fact_grounding_report(text, anchors)
    text = _source_faithful(text, source, voice, draft, user_id)
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
    thin = thin_commentary_reason(text)
    if thin:
        log_info("Curated commentary too thin to carry a take — not drafted", user_id=user_id,
                 chars=len(text), reason=thin, task_name="curated_commentary")
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


_AUDIENCE_FIT_PROMPT = """You decide whether an outside article is worth a LinkedIn author's \
commentary for THEIR readers. Rate 0-10 how directly the article matters to the readers described \
below: 10 = squarely about their work and decisions, 5 = useful with a clear link, 0 = about \
someone else entirely. Return JSON: {"score": <0-10>, "why": "<one short sentence>"}."""


@llm_step("curated_audience_fit")
def score_audience_fit(source: dict, brief: Optional[dict]) -> Optional[float]:
    """ONE cheap call: how relevant ``source`` is to the author's readers, 0-10. None on failure.

    Args:
        source: The curated-source row (its title and excerpt are read).
        brief: ``curated_sources.audience_brief(prefs)``.

    Returns:
        The score, or None when the call failed or answered nothing usable — the caller then falls
        back to ``curated_sources.audience_token_fit``.
    """
    if not brief:
        return None
    readers = "\n".join([f"- Reader: {a}" for a in brief.get("audiences") or []]
                        + [f"- Topic they follow: {t}" for t in brief.get("topics") or []])
    article = (f"Title: {source.get('title') or ''}\nPublisher: {source.get('publisher') or ''}\n"
               f"Excerpt: {(source.get('excerpt') or '')[:1500]}")
    try:
        raw = _complete([{"role": "system", "content": _AUDIENCE_FIT_PROMPT},
                         {"role": "user", "content": f"{readers}\n\n<article>{article}</article>"}],
                        model="lem-simple", json_mode=True)
        score = float(json.loads(raw or "{}").get("score"))
    except Exception as e:
        log_warning("Could not score a curated source's audience fit — using the token fallback",
                    exc=e, task_name="curated_commentary")
        return None
    return max(0.0, min(10.0, score))


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
    except Exception as e:
        log_warning("Could not read chart figures from a curated source", exc=e,
                    task_name="curated_commentary")
        return {}
    from cqc_lem.utilities.ai.ai_helper import _loads_json_object

    # Showcase round 8: a lem-simple json_mode reply that was not bare JSON raised JSONDecodeError
    # here. The repo's tolerant parse takes a fenced or prose-wrapped object; nothing parseable is
    # "no chart" (the post falls back to a link), which is expected, not a fault.
    data = _loads_json_object(raw)
    if data is None:
        log_debug("Chart figures reply carried no JSON object — no re-chart",
                  task_name="curated_commentary")
        return {}
    return data


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
