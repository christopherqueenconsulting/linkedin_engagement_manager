"""Story bank / fact intake (issue #620) — the FACT layer of the shared content core.

`profiles.synthesis` (content_alignment) tells a generator how the author SOUNDS. Nothing told it
what the author has actually DONE, so every "personal proof" specific was either lifted from a
profile blurb or quietly invented — the exact generic-AI signature LinkedIn's 2026 authenticity
ranking demotes. This module is the other half: the user's own anecdotes, numbers, opinions, wins,
mistakes and artifacts (`story_bank`, per-user), one of which is selected per post and handed to the
writer as the ONLY personal specifics it is allowed to state.

Everything here is a PURE function of rows + prompt context — no DB, no LLM — so selection,
rotation, the empty-bank fallback and the fabricated-specific detector are all unit-testable. The
DB reads/writes live in `utilities.db`; the wiring lives in `app.run_content_plan`.
"""

import os
import re
from datetime import date, datetime
from typing import Optional

from cqc_lem.utilities.ai.content_framework import content_tokens, first_person_proof_sentences
from cqc_lem.utilities.ai.slop_lint import SEVERITY_HARD, SEVERITY_OFF, SEVERITY_WARN

# How many content tokens an entry must share with the post's subject/focus topics to count as
# "relevant". One shared topic word is a deliberately low bar: the alternative to using the user's
# real material is inventing something, so a loose match beats no story at all.
STORY_RELEVANCE_MIN_TOKENS_DEFAULT = 1

# How much of an entry's body rides into the prompt. Long enough for a real anecdote, short enough
# that the story never crowds out the rest of the writer directive.
STORY_BODY_PROMPT_CHARS = 700

_KIND_LABELS: dict = {
    "anecdote": "a lived anecdote",
    "number": "a real number from their own work",
    "opinion": "a first-hand opinion they actually hold",
    "client_win": "a real client outcome",
    "mistake": "a mistake they actually made",
    "artifact": "something they actually built or shipped",
}


def relevance_min_tokens() -> int:
    """Read at call time (the POST_SIMILARITY_MAX live-env pattern) so ops can loosen or tighten the
    match without a restart.
    """
    try:
        return max(0, int(os.getenv("STORY_RELEVANCE_MIN_TOKENS",
                                    STORY_RELEVANCE_MIN_TOKENS_DEFAULT)))
    except (TypeError, ValueError):
        return STORY_RELEVANCE_MIN_TOKENS_DEFAULT


def entry_text(entry: dict) -> str:
    """The full searchable/citable text of one bank entry."""
    if not isinstance(entry, dict):
        return ""
    return " ".join(str(entry.get(k) or "").strip() for k in ("title", "body")).strip()


def topic_tokens(subject: Optional[str] = None, focus_topics: Optional[list] = None) -> set:
    """The content tokens describing what this post is about. Empty = no topic signal at all."""
    return content_tokens(" ".join(
        [str(subject or "")] + [str(t) for t in (focus_topics or [])]))


def relevance_score(entry: dict, subject: Optional[str] = None,
                    focus_topics: Optional[list] = None) -> int:
    """Shared content tokens between an entry and what this post is about. 0 = unrelated."""
    wanted = topic_tokens(subject, focus_topics)
    if not wanted:
        return 0
    return len(wanted & content_tokens(entry_text(entry)))


def _rotation_key(entry: dict) -> tuple:
    """Least-used, longest-unused first. A never-used entry always outranks a used one, so a freshly
    seeded bank is drained before anything repeats.
    """
    last_used = entry.get("last_used_at")
    if isinstance(last_used, datetime):
        stamp = last_used.timestamp()
    elif isinstance(last_used, date):
        stamp = datetime(last_used.year, last_used.month, last_used.day).timestamp()
    else:
        stamp = float("-inf")
    return (int(entry.get("used_count") or 0), stamp, int(entry.get("id") or 0))


def select_story(entries: Optional[list], subject: Optional[str] = None,
                 focus_topics: Optional[list] = None,
                 min_relevance: Optional[int] = None) -> Optional[dict]:
    """The one entry this post is anchored to, or None when the bank can't ground it.

    Relevance decides WHICH entries are eligible; rotation decides which eligible one is used, so
    the same anecdote never anchors three posts in a row. Returns None for an empty/inactive bank
    and for a bank whose entries share nothing with the post's subject or the user's focus topics —
    both cases mean the caller must fall back to a non-story archetype rather than invent
    experience the user never had.
    """
    usable = [e for e in (entries or []) if isinstance(e, dict) and entry_text(e)
              and e.get("active", True)]
    if not usable:
        return None
    threshold = relevance_min_tokens() if min_relevance is None else min_relevance
    wanted = topic_tokens(subject, focus_topics)
    if not wanted or threshold <= 0:
        # No topic signal at all means nothing can be off-topic — rotate over the whole bank rather
        # than blocking on a match that could never be made.
        eligible = usable
    else:
        eligible = [e for e in usable
                    if len(wanted & content_tokens(entry_text(e))) >= threshold]
    if not eligible:
        return None
    return sorted(eligible, key=_rotation_key)[0]


def _happened_phrase(entry: dict) -> str:
    happened = entry.get("happened_at")
    if isinstance(happened, (datetime, date)):
        return f" It happened on {happened.strftime('%B %d, %Y')} — you may reference that timing."
    happened = str(happened or "").strip()
    return f" It happened on {happened} — you may reference that timing." if happened else ""


def story_directive(entry: Optional[dict]) -> str:
    """The writer-side injection: the author's real material, plus the hard rule that it is the ONLY
    personal specific allowed. Returns the non-story fallback when there is no entry.
    """
    if not entry or not entry_text(entry):
        return no_story_directive()
    kind = str(entry.get("kind") or "anecdote")
    label = _KIND_LABELS.get(kind, "a lived detail")
    body = str(entry.get("body") or "").strip()[:STORY_BODY_PROMPT_CHARS]
    title = str(entry.get("title") or "").strip()
    return (
        "\n\nYOUR STORY BANK ENTRY (the author's OWN material — this is the post's factual anchor):\n"
        f"- Kind: {label}.\n"
        + (f"- Title: {title}\n" if title else "")
        + f"- What actually happened: {body}{_happened_phrase(entry)}\n"
        "- Build the personal-proof slot out of THIS entry, in the first person, with its real "
        "specifics (the numbers, names, dates and outcomes above).\n"
        "- ABSOLUTE RULE: these facts are the ONLY personal specifics you may state. Do not add, "
        "round, embellish or invent any other number, client, date, result or anecdote about the "
        "author. If a detail is not above, leave it out rather than making one up.\n")


def no_story_directive() -> str:
    """Empty-bank / no-relevant-entry fallback: write the observation post, and say NOTHING personal
    that we cannot back with a real entry. Inventing experience is the failure mode this whole
    module exists to remove, so the fallback closes the door explicitly.
    """
    return (
        "\n\nNO STORY BANK ENTRY IS AVAILABLE FOR THIS POST:\n"
        "- Write this as an industry observation or analysis grounded ONLY in the research and "
        "profile material already provided above.\n"
        "- ABSOLUTE RULE: do NOT invent a personal anecdote, client story, result, or number about "
        "the author. No 'last year I helped a client…', no made-up percentages. Ground the post in "
        "the sourced material instead, and let the author's stated expertise carry the credibility.\n")


# --- Fabricated-specific detection -------------------------------------------------------------
# The deterministic (no-LLM) counterpart to the directive above: a specific the draft states in the
# FIRST PERSON that appears nowhere in the material we gave it is, by definition, invented. Only run
# when a story entry was actually selected — with no entry there is no defined allow-list, so every
# number would look fabricated and the check would be noise.

_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
}
# Digit runs, with the grouping/decimal punctuation stripped so "1,200" and "1200" compare equal.
_DIGIT_RUN_RE = re.compile(r"\d[\d,.]*")
_WORD_RE = re.compile(r"[a-z]+")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december")


def _normalize_digits(raw: str) -> str:
    cleaned = raw.replace(",", "").rstrip(".")
    return cleaned.lstrip("0") or "0"


def specific_tokens(text: Optional[str]) -> set:
    """The checkable particulars in a piece of text: numbers (digits or spelled out, normalized to
    one form) and named months. These are what a reader could look up — and what an LLM invents.
    """
    out = set()
    low = (text or "").lower()
    for match in _DIGIT_RUN_RE.findall(low):
        out.add(_normalize_digits(match))
    for word in _WORD_RE.findall(low):
        if word in _NUMBER_WORDS:
            out.add(_NUMBER_WORDS[word])
        elif word in _MONTHS:
            out.add(word)
    return out


def unsourced_specifics(content: Optional[str], sources: Optional[list]) -> list:
    """Specifics the draft claims about the author that no source we supplied contains.

    Scoped to the draft's FIRST-PERSON sentences on purpose: a statistic quoted from the research
    layer ("the market grew 12%") is sourced elsewhere and is not a fabricated personal claim, while
    "I cut our onboarding from 12 days to 3" is only true if those numbers came from the bank.
    """
    allowed = set()
    for source in sources or []:
        allowed |= specific_tokens(source)
    found = []
    for sentence in first_person_proof_sentences(content):
        for token in sorted(specific_tokens(sentence)):
            if token not in allowed and token not in found:
                found.append(token)
    return found


def has_unsourced_specifics(content: Optional[str], sources: Optional[list]) -> bool:
    """True when the draft states a first-person specific we never gave it — i.e. it fabricated."""
    return bool(unsourced_specifics(content, sources))


# Severity of the unsourced-specific check, PER SURFACE — the `slop_lint.SURFACE_SEVERITIES`
# pattern, and the same vocabulary, because the question is the same one: how often is this check
# WRONG about a good draft, and what does a wrong verdict cost on THIS surface?
#
# COMMENTS are HARD (issue #1834). A comment publishes under the user's name the moment it is
# drafted — no review queue, no approval step, no edit before it lands — so the only two outcomes
# are "ship it" and "skip this post". A trace audit found invented first-person metrics ("we logged
# 1,200 errors per week, then 300, a 75% drop") in roughly 8 of 12 drafts read, and those read as
# the user's own operating history to everyone in the thread. A wrong block costs one comment on
# one post; a wrong pass costs a public, unretractable claim about the user's business.
#
# The known false positive is a spelled quantity counting nothing the sources mention — "in my
# experience three things matter". It costs one bounded regeneration (`comment_gate_max_attempts`
# caps the spend) and the retry directive names the token, so the rewrite drops it rather than
# paraphrasing around it. That is the trade HARD is buying.
#
# Everything else stays WARN. A POST is graded by the review gate and repaired by the #1134 editor
# loop, which already runs this same check against the story bank and then HOLDS the post at
# PENDING for a human — a second HARD verdict here would block the draft that path exists to fix.
FACT_GROUNDING_SEVERITIES: dict = {
    "comment": SEVERITY_HARD,
}
FACT_GROUNDING_SEVERITY_DEFAULT = SEVERITY_WARN


def fact_grounding_severity(content_type: Optional[str] = None) -> str:
    """Resolve this surface's verdict when a draft states an unsourced first-person specific.

    'hard' regenerates and then blocks, 'warn' records it and ships anyway, 'off' skips the check.
    Resolved most-specific-first so ops can overrule a built-in without a deploy:
    `FACT_GROUNDING_SEVERITY_<SURFACE>` beats the global `FACT_GROUNDING_SEVERITY`, which beats
    `FACT_GROUNDING_SEVERITIES` and then the WARN default. Read at call time, like every other
    severity knob in the content core.

    Args:
        content_type: The surface being graded ('comment', 'post', ...). An unknown or missing
            surface takes the default.

    Returns:
        One of `SEVERITY_HARD`, `SEVERITY_WARN` or `SEVERITY_OFF`.
    """
    surface = str(content_type or "").strip().lower()
    names = [f"FACT_GROUNDING_SEVERITY_{surface.upper()}"] if surface else []
    names.append("FACT_GROUNDING_SEVERITY")
    for name in names:
        raw = (os.environ.get(name) or "").strip().lower()
        if raw in (SEVERITY_HARD, SEVERITY_WARN, SEVERITY_OFF):
            return raw
    return FACT_GROUNDING_SEVERITIES.get(surface, FACT_GROUNDING_SEVERITY_DEFAULT)


def fabrication_repair_directive(tokens: Optional[list]) -> str:
    """Regeneration steer naming the exact specifics the last draft invented, so the rewrite drops
    them instead of paraphrasing them.
    """
    listed = ", ".join(str(t) for t in (tokens or []) if str(t).strip())
    return (
        "\n\nTHE PREVIOUS DRAFT INVENTED FACTS ABOUT THE AUTHOR — do NOT repeat that:\n"
        + (f"- These specifics appeared nowhere in the author's material: {listed}.\n" if listed
           else "")
        + "- Use ONLY the numbers, dates, names and outcomes from the story bank entry above. If "
          "you need a specific that is not there, write the sentence without one.\n")


def fact_sources(entry: Optional[dict], *extra: Optional[str]) -> list:
    """Everything the writer was legitimately allowed to draw a specific from."""
    sources = [entry_text(entry)] if entry else []
    happened = (entry or {}).get("happened_at")
    if isinstance(happened, (datetime, date)):
        sources.append(happened.strftime("%B %d %Y %m %d"))
    elif happened:
        sources.append(str(happened))
    sources += [str(x) for x in extra if x]
    return [s for s in sources if s]
