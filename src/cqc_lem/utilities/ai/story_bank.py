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

from cqc_lem.utilities.ai.content_framework import (
    content_tokens,
    first_person_proof_sentences,
    numeric_claims,
)
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
# POSTS are HARD too (issue #1971). The earlier premise — "the review gate already holds them" —
# was false in production: three generated posts carrying invented figures ("≈45% lower
# cost-per-call", "AI inference costs dropped 30% in Q2 2026", "41 PRs in a day") published to the
# operator's own profile, because the first-person-only detector never fired on a third-person
# industry claim and the number gate ran only on the two fact-anchored archetypes. A human byline
# is the argument FOR hard, not against it: a preview queue trains its reviewer to trust it. At HARD
# every post's numbers are graded by `content_framework.fact_grounding_report` against the user's
# whole verified material (story bank, profile, the research actually supplied), repaired once by
# the editor, and then HELD at PENDING with the offending numbers named — never published.
#
# Everything else stays WARN. `FACT_GROUNDING_SEVERITY_POST=warn` restores the pre-#1971 posture
# without a deploy.
FACT_GROUNDING_SEVERITIES: dict = {
    "comment": SEVERITY_HARD,
    "post": SEVERITY_HARD,
}
FACT_GROUNDING_SEVERITY_DEFAULT = SEVERITY_WARN

# Forbidden claims (issue #1971): a specific the author has said must NEVER be attached to a
# subject, however well-grounded the draft looks. A claim can be ungrounded in general AND
# specifically forbidden for a reason the story bank cannot know — the case that filed the issue was
# a router whose config meters its targets at zero, so ANY cost or latency figure quoted about it is
# invented by construction. Checked at HARD before grounding is even consulted, on the two gated
# surfaces — posts (`evaluate_post_gates`) and comments (`_gated_comment`); the newsletter,
# group-post and DM writers are not covered yet.
#
# `FORBIDDEN_CLAIM_TERMS` is a `;`-separated list of phrases. A phrase matches a draft when it
# appears anywhere in it as WHOLE WORDS (case-insensitive, punctuation folded to spaces, so
# "cost per call" matches "cost-per-call" and "ai" never matches "said") AND the draft asserts at
# least one numeric claim — the term names the SUBJECT, the numbers are the claims being forbidden.
# The window is the whole draft on purpose: the post that filed the issue named the router in its
# first sentence and quoted its "≈45% lower cost-per-call" two sentences later, so a same-sentence
# rule would have waved it through. A wrong match costs one human review, never a publish.
#
# Two lists, ONE effective list (issue #2047): the env variable is the global FLOOR every user gets,
# and `engagement_preferences.forbidden_claim_terms` is the user's own. `effective_forbidden_claim_terms`
# is the only place they meet — the user's terms PLUS the global ones, never instead of — so a
# per-user list can widen what is forbidden and can never narrow it.
_FORBIDDEN_TERMS_ENV = "FORBIDDEN_CLAIM_TERMS"
_FOLD_RE = re.compile(r"[^a-z0-9]+")
# Bounds on the per-user list, enforced at the API boundary AND in the repository upsert (the V52
# lesson: the engagement row is ONE upsert, so a single bad value rolls back every section). 50
# subjects is far past any real list; 80 chars holds a product name plus a qualifier.
FORBIDDEN_CLAIM_TERMS_MAX = 50
FORBIDDEN_CLAIM_TERM_MAX_LEN = 80


def _fold(text: str) -> str:
    """Lower-case with every punctuation/whitespace run folded to ONE space, space-padded."""
    return " " + _FOLD_RE.sub(" ", str(text or "").lower()).strip() + " "


def normalize_forbidden_claim_terms(values: Optional[list]) -> list:
    """The per-user forbidden-claim list as it is STORED — bounded, tidy, and never a surprise.

    Args:
        values: What the client or caller handed over. Anything that is not a list is treated as
            an empty one, so a malformed value can never fail the whole settings save.

    Returns:
        Each term whitespace-normalised (runs collapsed to one space, ends trimmed) in the order
        given, with empties dropped, terms over `FORBIDDEN_CLAIM_TERM_MAX_LEN` dropped (a clipped
        subject would never match the text it was meant to catch), duplicates on the folded form
        dropped (the first spelling wins), and the list cut at `FORBIDDEN_CLAIM_TERMS_MAX`.
    """
    if not isinstance(values, (list, tuple)):
        return []
    out: list = []
    seen: set = set()
    for value in values:
        if value is None:
            continue
        term = " ".join(str(value).split())
        key = _fold(term).strip()
        if not key or len(term) > FORBIDDEN_CLAIM_TERM_MAX_LEN or key in seen:
            continue
        seen.add(key)
        out.append(term)
        if len(out) >= FORBIDDEN_CLAIM_TERMS_MAX:
            break
    return out


def forbidden_claim_terms() -> list:
    """The operator's forbidden-claim subjects, read at call time from `FORBIDDEN_CLAIM_TERMS`.

    Returns:
        The non-empty phrases, whitespace-normalised and lower-cased, in the order configured.
        An unset or blank variable is an empty list, which forbids nothing.
    """
    raw = os.environ.get(_FORBIDDEN_TERMS_ENV) or ""
    out = []
    for part in raw.split(";"):
        term = _fold(part).strip()
        if term and term not in out:
            out.append(term)
    return out


def effective_forbidden_claim_terms(user_terms: Optional[list] = None) -> list:
    """The ONE list a draft is checked against: the user's own subjects PLUS the global floor.

    The env list is read here, at call time, so an ops change lands without a restart, and it is
    always included — a user's list widens what is forbidden and can never narrow it.

    Args:
        user_terms: The user's `forbidden_claim_terms` preference, as stored. `None` or anything
            that is not a list means the user has none.

    Returns:
        The user's terms first, then the global ones, each folded (lower-cased, punctuation and
        whitespace runs collapsed) and listed once. Empty forbids nothing.
    """
    out: list = []
    candidates = list(user_terms) if isinstance(user_terms, (list, tuple)) else []
    for raw in candidates + forbidden_claim_terms():
        term = _fold(raw).strip()
        if term and term not in out:
            out.append(term)
    return out


def forbidden_claims(content: Optional[str], terms: Optional[list] = None) -> list:
    """The forbidden subjects this draft attaches a number to.

    Args:
        content: The draft, exactly as it would ship.
        terms: The forbidden subjects; `None` reads them from the environment.

    Returns:
        Each matched term once, in configured order — the draft names the subject as whole words
        AND asserts a numeric claim (`content_framework.numeric_claims`: years, list numbering and
        version numbers are not claims). Empty when nothing is forbidden or nothing matched.
    """
    subjects = forbidden_claim_terms() if terms is None else [
        _fold(t).strip() for t in terms if _fold(t).strip()]
    if not subjects or not content:
        return []
    folded = _fold(content)
    named = [term for term in subjects if f" {term} " in folded]
    if not named or not numeric_claims(content):
        return []
    return named


def fact_grounding_severity(content_type: Optional[str] = None) -> str:
    """Resolve this surface's verdict when a draft states an unsourced specific.

    'hard' regenerates and then blocks (a comment is skipped, a post is held at PENDING), 'warn'
    records it and ships anyway, 'off' skips the check.
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
