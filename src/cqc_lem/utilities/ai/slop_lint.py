"""Deterministic AI-slop linter (issue #625 / D1) — ONE cheap, explainable lint layer that runs on
EVERY surface LEM publishes: posts, comments, DMs, newsletter editions, and group posts.

WHY THIS EXISTS (the #416 policy, restated so nobody re-reads it as something else): this linter is
here to make the writing genuinely BETTER. LinkedIn's May 2026 update names the patterns below as
ones it suppresses, and human readers have learned to skim straight past them. It is NOT an
AI-detector-evasion tool — nothing here fabricates a fact, plants a fake typo, or disguises who
wrote the text. Every violation is explainable in one plain-English sentence, and every fix is "say
the plain thing instead". DETECTOR-mode fracture stays out of the automated path exactly as #416
decided.

It complements, never replaces, the two LLM passes already in the pipeline. `humanize_text`
(#416 / A5) SAMPLES a rewrite and can miss; `score_authenticity` (#382 / A1) judges holistically and
costs a call. This layer is pure regex/string/statistics — no LLM, no network, no DB — so it runs on
every draft, always agrees with itself, and can name exactly which pattern it caught. Callers feed
the violations back into a bounded regeneration and then block with the reasons attached
(`quality_gates.slop_finding` for posts, a skip for comments).

The tier-1 word list is `content_alignment.AI_TELL_WORDS` — the SAME wordbank the humanization pass
steers against, so the writer side and the checking side can never drift apart.
"""

import os
import re
import statistics
from typing import Optional

from cqc_lem.utilities.ai.content_alignment import AI_TELL_WORDS
from cqc_lem.utilities.ai.content_framework import (
    NEWSLETTER_BANNED_SCAFFOLDS,
    POST_BANNED_SCAFFOLDS,
)
from cqc_lem.utilities.linkedin_formatter import contains_engagement_bait

SEVERITY_HARD = "hard"
SEVERITY_WARN = "warn"
SEVERITY_OFF = "off"

CHECK_LEXICON = "banned_lexicon"
CHECK_CONTRASTIVE = "contrastive_frame"
CHECK_TADA = "tada_transition"
CHECK_BAIT_CLOSER = "bait_closer"
CHECK_EMOJI_BULLETS = "emoji_bullets"
CHECK_EM_DASH = "em_dash_density"
CHECK_RULE_OF_THREE = "rule_of_three"
CHECK_BURSTINESS = "burstiness"
CHECK_RHETORICAL_HOOK = "rhetorical_hook"
CHECK_SCAFFOLD = "canned_scaffold"

# Default severities. HARD violations are regenerated and then block; WARN ones are recorded and
# reported but never hold a draft.
#
# The split is about how often a check is WRONG about a good human draft, not about how bad the
# pattern is. The hard five are unambiguous machine tells with a one-line fix. The warn five are
# statistical or structural signals with real false positives — a legitimate list of three tools
# reads exactly like a rule-of-three, a genuinely short post has no sentence-length variance to
# measure, a question hook can be the right opening, and "in my experience as a Solutions
# Architect, we cut deploys to 9 minutes" is a templated OPENER carrying a real specific, which no
# substring match can tell from filler. Ops can promote any of them per-deploy with
# SLOP_LINT_SEVERITY_<CHECK>.
DEFAULT_SEVERITIES: dict = {
    CHECK_LEXICON: SEVERITY_HARD,
    CHECK_CONTRASTIVE: SEVERITY_HARD,
    CHECK_TADA: SEVERITY_HARD,
    CHECK_BAIT_CLOSER: SEVERITY_HARD,
    CHECK_EMOJI_BULLETS: SEVERITY_HARD,
    CHECK_EM_DASH: SEVERITY_WARN,
    CHECK_RULE_OF_THREE: SEVERITY_WARN,
    CHECK_BURSTINESS: SEVERITY_WARN,
    CHECK_RHETORICAL_HOOK: SEVERITY_WARN,
    CHECK_SCAFFOLD: SEVERITY_WARN,
}

# How many DISTINCT tier-1 tell words a draft may carry before the lexicon check fires. Not zero on
# purpose: the rubric's own calibration principle is that any single tell is weak evidence (a real
# engineer does say "optimize"), while a pileup is the signal. `> LEXICON_MAX` fires, so the default
# blocks at three distinct tells.
LEXICON_MAX_DEFAULT = 2
# Em-dash tells per sentence. The humanization pass already caps a rewrite at ONE em-dash, so this
# only fires on genuinely dash-riddled text (or a draft that skipped humanization).
EM_DASH_PER_SENTENCE_DEFAULT = 0.34
# Population stdev of per-sentence word counts below which the text is a "perfect rectangle" — every
# sentence the same length, the single most reliable machine tell in long prose.
BURSTINESS_MIN_DEFAULT = 4.0
# Burstiness is meaningless on a two-sentence comment; below this it is skipped entirely.
BURSTINESS_MIN_SENTENCES = 5
# Emoji-led lines allowed before it reads as an emoji-bullet listicle.
EMOJI_BULLET_MAX_DEFAULT = 2
# Total drafts one caller may spend on a piece (initial + regenerations).
MAX_ATTEMPTS_DEFAULT = 2

# Multi-word slop the tier-1 wordbank cannot catch (it is word-level). Kept deliberately tight —
# every entry here is a phrase a careful human writer would cut anyway.
SLOP_PHRASES: tuple = (
    "in today's fast-paced world", "in today's world", "in today's digital age",
    "in the ever-evolving", "in an ever-changing", "in the age of ai", "in the world of",
    "it's important to note", "it is important to note", "it's worth noting",
    "in conclusion", "in summary", "to sum up", "needless to say",
    "let's dive in", "let's dive into", "let's unpack", "buckle up",
    "game changer", "the fact of the matter is", "the harsh truth is",
    "look no further", "the possibilities are endless",
)

# The "ta-da" transition: a manufactured beat that promises a payoff the next sentence rarely earns.
TADA_TRANSITIONS: tuple = (
    "here's the kicker", "here's the thing", "here's the catch", "here's the twist",
    "here's where it gets", "here's what nobody", "here's the part nobody",
    "but here's the thing", "but wait, there's more", "plot twist", "spoiler alert",
    "let that sink in", "and that changed everything", "the crazy part",
)

# The same beat as a one-word rhetorical fragment ("The result? ..."). Matched against a WHOLE
# sentence, never mid-sentence: the tell is the manufactured two-word beat standing on its own, and
# an ordinary interrogative that happens to end on the same noun ("So what's the takeaway?",
# "What was the result?") is a real question a human asks.
_TADA_FRAGMENT_RE = re.compile(
    r"^[\s\-–—•*\"'“”]*(?:(?:and|but|so)\s+)?(?:the\s+)?"
    r"(?:result|kicker|catch|twist|payoff|punchline|takeaway|outcome|best\s+part|"
    r"bottom\s+line)\s*\?[\s\"'”]*$", re.IGNORECASE)

# The contrastive-negation frame LinkedIn's May 2026 crackdown names by hand. Three shapes:
# "not just X, it's Y", "X isn't Y. It's Z.", and "not only X but also Y".
#
# Both pronoun shapes require an explicit copula — the apostrophe-s contraction or is/are/was/were.
# Without it the pattern swallows ordinary prose: "I did not just read the docs, we ran the
# migration twice" is not the frame, and the possessive "its" ("the issue wasn't the API, its rate
# limiter fired at 100 rps") is not "it's".
_CONTRASTIVE_RES: tuple = (
    re.compile(r"\bnot\s+(?:just|only|merely|simply|about)\s+[^.!?\n;]{2,70}[,;:]\s*"
               r"(?:it|this|that|they|we)(?:\s*(?:'|’)s|\s+(?:is|are|was|were))\s+\w",
               re.IGNORECASE),
    re.compile(r"\b(?:is|are|was|were)\s?n(?:'|’)?t\s+(?:just\s+|only\s+|about\s+)?"
               r"[^.!?\n;]{2,70}[.,;:]\s*(?:it|this|that|they|we)\s*(?:'|’)s\b", re.IGNORECASE),
    re.compile(r"\bnot\s+only\b[^.!?\n]{2,80}\bbut\s+also\b", re.IGNORECASE),
)

# Reflex closers: an ask for a reply that gives the reader nowhere to go. Matched only in the
# CLOSING position — "Agree?" mid-argument is a rhetorical beat, "Agree?" as the last line is bait.
# (content_framework._GENERIC_QUESTIONS grades the same shapes for the comment contract's
# "genuine question" value-add; this is the closer-position half of the same idea.)
REFLEX_CLOSERS: frozenset = frozenset({
    "thoughts", "agree", "right", "am i wrong", "who's with me", "whos with me", "who else",
    "what do you think", "any thoughts", "make sense", "sound familiar", "are you ready",
    "what say you", "can you relate", "who agrees", "curious what others think",
})

# Shallow question hooks — the opening the model reaches for when it has no real first line.
_RHETORICAL_HOOK_RE = re.compile(
    r"^(?:ever\s+(?:wonder|noticed|felt|tried)|have\s+you\s+ever|what\s+if|did\s+you\s+know|"
    r"why\s+do\s+(?:so\s+many|most|we|you)|sound\s+familiar|who\s+else|what\s+would\s+happen|"
    r"imagine\s+(?:if|for\s+a))\b", re.IGNORECASE)

# Rule-of-three: three single-word items whose endings mark them as adjectives/adverbs/gerunds
# ("faster, smarter, better"). Constrained to those endings so a real list of three nouns
# ("Postgres, Redis, and Docker") is not flagged.
_TRIAD_RE = re.compile(r"\b([A-Za-z]{3,}),\s+([A-Za-z]{3,}),\s+(?:and\s+)?([A-Za-z]{3,})\b")
_TRIAD_SUFFIXES: tuple = ("er", "est", "ly", "ing", "ive", "ful", "ous", "able", "ible", "ent")

# Em-dash "—" and its "--" typing variant, the same token content_alignment.cap_em_dashes counts.
_EM_DASH_RE = re.compile(r"\s*(?:—|--)\s*")

_EMOJI_LINE_RE = re.compile(
    r"^\s*[\U0001F300-\U0001FAFF\U0001F000-\U0001F2FF←-➿⬀-⯿✅❌]")

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z-]*")
_SMART_PUNCTUATION = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})


# ---------------------------------------------------------------------------
# Config. Every value is read at CALL time (the POST_SIMILARITY_MAX live-env pattern) so ops can
# tune or disable a check without a restart.
# ---------------------------------------------------------------------------

def _env_flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_number(name: str, default: float, cast=float, low: float = None, high: float = None):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        return default
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def slop_lint_enabled(content_type: Optional[str] = None) -> bool:
    """Master + per-surface toggle, mirroring `humanize_enabled`. SLOP_LINT_ENABLED (default ON)
    gates everything; SLOP_LINT_<TYPE>_ENABLED (e.g. SLOP_LINT_COMMENT_ENABLED) turns one surface
    off without touching the others. SLOP_LINT_ENABLED=off restores the exact prior behavior.
    """
    if not _env_flag("SLOP_LINT_ENABLED", True):
        return False
    if content_type:
        raw = (os.environ.get(f"SLOP_LINT_{content_type.strip().upper()}_ENABLED") or "").strip().lower()
        if raw:
            return raw in ("1", "true", "yes", "on")
    return True


def check_severity(check: str) -> str:
    """This check's severity: 'hard' (regenerate, then block), 'warn' (report only), or 'off'.
    SLOP_LINT_SEVERITY_<CHECK> overrides the default, e.g. SLOP_LINT_SEVERITY_BURSTINESS=hard.
    """
    raw = (os.environ.get(f"SLOP_LINT_SEVERITY_{check.upper()}") or "").strip().lower()
    if raw in (SEVERITY_HARD, SEVERITY_WARN, SEVERITY_OFF):
        return raw
    return DEFAULT_SEVERITIES.get(check, SEVERITY_WARN)


def lexicon_max() -> int:
    """Distinct tier-1 tells tolerated before the lexicon check fires (`SLOP_LINT_LEXICON_MAX`).

    The comparison is strictly greater-than, so the value is the last COUNT that still passes.
    Re-read on every call, so ops can retune a check without a restart.
    """
    return int(_env_number("SLOP_LINT_LEXICON_MAX", LEXICON_MAX_DEFAULT, int, low=0))


def em_dash_per_sentence_max() -> float:
    """Ceiling on em-dash DENSITY, not count (`SLOP_LINT_EM_DASH_PER_SENTENCE`).

    A density, so the same two dashes pass in a long draft and fire in a three-sentence comment.
    Re-read on every call.
    """
    return float(_env_number("SLOP_LINT_EM_DASH_PER_SENTENCE", EM_DASH_PER_SENTENCE_DEFAULT,
                             float, low=0.0))


def burstiness_min() -> float:
    """A FLOOR, unlike its neighbours here — sentence-length spread below it fires the check.

    `SLOP_LINT_BURSTINESS_MIN`, re-read on every call. Raising it makes the check stricter, which is
    the opposite of the `*_max` knobs.
    """
    return float(_env_number("SLOP_LINT_BURSTINESS_MIN", BURSTINESS_MIN_DEFAULT, float, low=0.0))


def emoji_bullet_max() -> int:
    """Emoji-LED lines tolerated before a draft reads as a listicle (`SLOP_LINT_EMOJI_BULLET_MAX`).

    Counts lines that START with an emoji, not emoji anywhere in the text — an emoji mid-sentence is
    never what this check is about. Re-read on every call.
    """
    return int(_env_number("SLOP_LINT_EMOJI_BULLET_MAX", EMOJI_BULLET_MAX_DEFAULT, int, low=0))


def slop_max_attempts() -> int:
    """Total drafts a caller may spend on one piece (initial + regenerations)."""
    return int(_env_number("SLOP_LINT_MAX_ATTEMPTS", MAX_ATTEMPTS_DEFAULT, int, low=1, high=5))


def banned_words() -> frozenset:
    """The tier-1 wordbank, extended by SLOP_LINT_EXTRA_WORDS and reduced by SLOP_LINT_ALLOW_WORDS
    (both comma-separated). The allow-list exists because a word that is slop in general prose can
    be a term of art in one author's niche ("optimize" for a performance engineer).
    """
    extra = {w.strip().lower() for w in (os.environ.get("SLOP_LINT_EXTRA_WORDS") or "").split(",")}
    allow = {w.strip().lower() for w in (os.environ.get("SLOP_LINT_ALLOW_WORDS") or "").split(",")}
    return frozenset((set(AI_TELL_WORDS) | {w for w in extra if w}) - {w for w in allow if w})


def banned_phrases() -> tuple:
    """SLOP_PHRASES, extended per-deploy via SLOP_LINT_EXTRA_PHRASES (comma-separated)."""
    extra = [p.strip().lower() for p in (os.environ.get("SLOP_LINT_EXTRA_PHRASES") or "").split(",")]
    return SLOP_PHRASES + tuple(p for p in dict.fromkeys(extra) if p and p not in SLOP_PHRASES)


def banned_scaffolds() -> tuple:
    """The banned-scaffold list, extended per-deploy via `SLOP_LINT_EXTRA_SCAFFOLDS`.

    `content_framework.POST_BANNED_SCAFFOLDS` + `NEWSLETTER_BANNED_SCAFFOLDS`.
    Never a second list: the shared constants are what the writer-side directives name, so a phrase
    can only be banned in the prompt and unchecked here (or the reverse) by editing it away.
    """
    extra = [p.strip().lower()
             for p in (os.environ.get("SLOP_LINT_EXTRA_SCAFFOLDS") or "").split(",")]
    base = POST_BANNED_SCAFFOLDS + NEWSLETTER_BANNED_SCAFFOLDS
    return base + tuple(p for p in dict.fromkeys(extra)
                        if p and p not in base)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _plain(text: Optional[str]) -> str:
    return (text or "").translate(_SMART_PUNCTUATION).lower()


def sentences(text: Optional[str]) -> list:
    """The draft split into sentences — shared by the density and burstiness checks so they always
    grade the same units.
    """
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text or "") if s.strip()]


def _excerpt(text: str, limit: int = 120) -> str:
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# The checks. Each returns a violation body ({detail, evidence, score, threshold}) or None.
# ---------------------------------------------------------------------------

def find_banned_lexicon(text: Optional[str]) -> list:
    """Tier-1 tell words + slop phrases present in `text`, in order of first appearance, deduped."""
    plain = _plain(text)
    words, seen = [], set()
    allowed = banned_words()
    for tok in _WORD_TOKEN_RE.findall(plain):
        if tok in allowed and tok not in seen:
            seen.add(tok)
            words.append(tok)
    phrases = [p for p in banned_phrases() if p in plain]
    return words + phrases


def _check_lexicon(text: str, sents: list, ctx: dict) -> Optional[dict]:
    hits = find_banned_lexicon(text)
    ceiling = lexicon_max()
    if len(hits) <= ceiling:
        return None
    return {"detail": (f"uses {len(hits)} AI-tell words/phrases (max {ceiling}): "
                       + ", ".join(hits[:10])),
            "evidence": hits[:10], "score": float(len(hits)), "threshold": float(ceiling)}


def _check_contrastive(text: str, sents: list, ctx: dict) -> Optional[dict]:
    hits = []
    for rx in _CONTRASTIVE_RES:
        hits += [_excerpt(m.group(0)) for m in rx.finditer(text or "")]
    if not hits:
        return None
    return {"detail": ("uses the \"it's not X, it's Y\" contrastive frame, the construction "
                       "LinkedIn's 2026 update names first: " + "; ".join(hits[:3])),
            "evidence": hits[:5], "score": float(len(hits)), "threshold": 0.0}


def _check_tada(text: str, sents: list, ctx: dict) -> Optional[dict]:
    plain = _plain(text)
    hits = [p for p in TADA_TRANSITIONS if p in plain]
    hits += [_excerpt(s, 60) for s in sents if _TADA_FRAGMENT_RE.match(s)]
    if not hits:
        return None
    return {"detail": ("uses manufactured \"ta-da\" transitions that promise a payoff: "
                       + ", ".join(hits[:5])),
            "evidence": hits[:5], "score": float(len(hits)), "threshold": 0.0}


def closing_reflex_ask(text: Optional[str]) -> Optional[str]:
    """The reflex closer this draft ends on ("Thoughts?", "Agree?"), or None. Only the LAST
    sentence counts — the same question mid-post is a rhetorical beat, not a bait close.
    """
    sents = sentences(text)
    if not sents:
        return None
    last = _plain(sents[-1]).strip().strip("\"'.!? ")
    return last if last in REFLEX_CLOSERS else None


def bait_lines(text: Optional[str], exempt_keyword: Optional[str] = None) -> list:
    """The engagement-bait lines in `text`, using the SAME line-level exemption
    `linkedin_formatter.strip_engagement_bait` applies: a line carrying the user's configured
    lead-magnet trigger word is a sanctioned "comment KEYWORD" CTA, not bait — even when that word
    ("YES") collides with the bait regex.
    """
    kw = str(exempt_keyword or "").strip()
    kw_re = re.compile(rf"(?<!\w){re.escape(kw)}(?!\w)", re.IGNORECASE) if kw else None
    return [ln for ln in str(text or "").splitlines()
            if contains_engagement_bait(ln) and not (kw_re is not None and kw_re.search(ln))]


def _check_bait_closer(text: str, sents: list, ctx: dict) -> Optional[dict]:
    hits = []
    closer = closing_reflex_ask(text)
    if closer:
        hits.append(f"closes on \"{closer}?\"")
    if bait_lines(text, ctx.get("exempt_keyword")):
        hits.append("asks for a reflex action (like/tag/follow/one-word reply)")
    if not hits:
        return None
    return {"detail": ("ends on engagement bait rather than a question worth answering: "
                       + "; ".join(hits)),
            "evidence": hits, "score": float(len(hits)), "threshold": 0.0}


def _check_emoji_bullets(text: str, sents: list, ctx: dict) -> Optional[dict]:
    lines = [ln for ln in (text or "").splitlines() if _EMOJI_LINE_RE.match(ln)]
    ceiling = emoji_bullet_max()
    if len(lines) <= ceiling:
        return None
    return {"detail": (f"is an emoji-bulleted listicle ({len(lines)} emoji-led lines, max "
                       f"{ceiling}) — one abstract tip per bullet reads as machine-generated"),
            "evidence": [_excerpt(ln, 60) for ln in lines[:5]],
            "score": float(len(lines)), "threshold": float(ceiling)}


def em_dash_density(text: Optional[str]) -> float:
    """Em-dash tells per sentence. 0.0 for empty text."""
    sents = sentences(text)
    if not sents:
        return 0.0
    return len(_EM_DASH_RE.findall(text or "")) / len(sents)


def _check_em_dash(text: str, sents: list, ctx: dict) -> Optional[dict]:
    count = len(_EM_DASH_RE.findall(text or ""))
    if not count or not sents:
        return None
    density = count / len(sents)
    ceiling = em_dash_per_sentence_max()
    if density <= ceiling:
        return None
    return {"detail": (f"leans on em-dashes ({count} across {len(sents)} sentences, "
                       f"{density:.2f}/sentence vs max {ceiling:.2f}) — use a comma or a period"),
            "evidence": [], "score": round(density, 4), "threshold": round(ceiling, 4)}


def find_rule_of_three(text: Optional[str]) -> list:
    """Rhythm-built triads ("faster, smarter, better"). Only flags triads whose three items share an
    adjective/adverb/gerund ending, so an ordinary list of three nouns is left alone.
    """
    hits = []
    for m in _TRIAD_RE.finditer(text or ""):
        items = [g.lower() for g in m.groups()]
        if all(any(w.endswith(suf) for suf in _TRIAD_SUFFIXES) for w in items):
            hits.append(_excerpt(m.group(0), 60))
    return hits


def _check_rule_of_three(text: str, sents: list, ctx: dict) -> Optional[dict]:
    hits = find_rule_of_three(text)
    if not hits:
        return None
    return {"detail": "builds rule-of-three lists for rhythm rather than meaning: " + "; ".join(hits[:3]),
            "evidence": hits[:5], "score": float(len(hits)), "threshold": 0.0}


def burstiness(text: Optional[str]) -> Optional[float]:
    """Population stdev of per-sentence word counts, or None when the draft is too short to grade."""
    lengths = [len(s.split()) for s in sentences(text)]
    if len(lengths) < BURSTINESS_MIN_SENTENCES:
        return None
    return statistics.pstdev(lengths)


def _check_burstiness(text: str, sents: list, ctx: dict) -> Optional[dict]:
    spread = burstiness(text)
    if spread is None:
        return None
    floor = burstiness_min()
    if spread >= floor:
        return None
    return {"detail": (f"has near-uniform sentence lengths (spread {spread:.1f} < {floor:.1f}) — "
                       f"real writing mixes a very short sentence with a long one"),
            "evidence": [], "score": round(spread, 4), "threshold": round(floor, 4)}


def _check_rhetorical_hook(text: str, sents: list, ctx: dict) -> Optional[dict]:
    if not sents:
        return None
    first = sents[0].strip()
    if not first.endswith("?") or not _RHETORICAL_HOOK_RE.match(first):
        return None
    return {"detail": ("opens on a stock rhetorical question (\"" + _excerpt(first, 80)
                       + "\") — lead with the specific instead"),
            "evidence": [_excerpt(first, 120)], "score": 1.0, "threshold": 0.0}


def find_canned_scaffolds(text: Optional[str]) -> list:
    """The banned scaffold templates present in `text`, in list order, deduped.

    Whitespace is collapsed before matching, so a scaffold broken across a line break — which is how
    it looks in a wrapped prompt source file, and how a formatted post can read — still matches.
    """
    plain = " ".join(_plain(text).split())
    return [p for p in banned_scaffolds() if p in plain]


def _check_scaffold(text: str, sents: list, ctx: dict) -> Optional[dict]:
    # POST + NEWSLETTER ON PURPOSE. Every phrase in the list was sampled from LEM's own POST and
    # NEWSLETTER system prompts; comments already run their own filler-opener contract
    # (content_framework.comment_filler_openers) against a tighter, addressed voice — grading a
    # comment against the post/newsletter list would fire a second signal for the same idea on a
    # surface the evidence never measured.
    if ctx.get("content_type") not in ("post", "newsletter"):
        return None
    hits = find_canned_scaffolds(text)
    if not hits:
        return None
    surface = ctx.get("content_type", "post")
    return {"detail": (f"leans on canned scaffolding that would paste unchanged under any {surface}: "
                       + ", ".join(f'"{h}"' for h in hits[:5])),
            "evidence": hits[:5], "score": float(len(hits)), "threshold": 0.0}


_CHECKS: tuple = (
    (CHECK_LEXICON, _check_lexicon),
    (CHECK_CONTRASTIVE, _check_contrastive),
    (CHECK_TADA, _check_tada),
    (CHECK_BAIT_CLOSER, _check_bait_closer),
    (CHECK_EMOJI_BULLETS, _check_emoji_bullets),
    (CHECK_EM_DASH, _check_em_dash),
    (CHECK_RULE_OF_THREE, _check_rule_of_three),
    (CHECK_BURSTINESS, _check_burstiness),
    (CHECK_RHETORICAL_HOOK, _check_rhetorical_hook),
    (CHECK_SCAFFOLD, _check_scaffold),
)


def lint_report(text: Optional[str], content_type: str = "post",
                exempt_keyword: Optional[str] = None) -> dict:
    """Grade one finished draft against every slop check. Deterministic, no LLM, no I/O — the same
    draft always gets the same verdict.

    Returns {passes, violations, hard, warnings, reasons, checked}. `passes` is False only when a
    HARD-severity check fired; `warnings` are recorded for the review UI and the logs but never hold
    a draft. `checked` is False when the linter is disabled for this surface (or the text is empty),
    in which case the report is empty and passing — this layer fails OPEN, always.

    `exempt_keyword` is the user's lead-magnet trigger word: a "Comment YES" CTA is sanctioned, not
    bait, exactly as `strip_engagement_bait` treats it.
    """
    empty = {"passes": True, "violations": [], "hard": [], "warnings": [], "reasons": [],
             "checked": False}
    if not text or not str(text).strip():
        return empty
    if not slop_lint_enabled(content_type):
        return empty

    body = str(text)
    sents = sentences(body)
    ctx = {"exempt_keyword": exempt_keyword, "content_type": content_type}
    violations = []
    for name, fn in _CHECKS:
        severity = check_severity(name)
        if severity == SEVERITY_OFF:
            continue
        found = fn(body, sents, ctx)
        if not found:
            continue
        violations.append({"check": name, "severity": severity, **found})

    hard = [v for v in violations if v["severity"] == SEVERITY_HARD]
    warnings = [v for v in violations if v["severity"] == SEVERITY_WARN]
    return {"passes": not hard, "violations": violations, "hard": hard, "warnings": warnings,
            "reasons": [f"{v['check']}: {v['detail']}" for v in violations], "checked": True}


def passes_slop_lint(text: Optional[str], content_type: str = "post",
                     exempt_keyword: Optional[str] = None) -> bool:
    """True when no HARD slop check fired (or the linter is off for this surface)."""
    return lint_report(text, content_type, exempt_keyword)["passes"]


def violation_reasons(violations: Optional[list]) -> list:
    """Plain-English reason strings for a list of violations — what the review UI and the logs
    show.
    """
    return [f"{v.get('check')}: {v.get('detail')}" for v in (violations or [])
            if isinstance(v, dict) and v.get("detail")]


def slop_retry_directive(violations: Optional[list]) -> str:
    """The regeneration steer after a draft fails the lint: name each pattern that fired and what
    to do instead, so the retry fixes the actual construction rather than paraphrasing around it.
    """
    reasons = [v for v in (violations or []) if isinstance(v, dict) and v.get("detail")]
    if not reasons:
        return ""
    lines = ["\n\nYOUR PREVIOUS DRAFT TRIPPED THE AI-SLOP LINT. Rewrite it so none of these remain "
             "— keep every fact and the same intent, change only the writing:"]
    for v in reasons:
        lines.append(f"- It {v['detail']}.")
    lines.append("- Say the plain thing you would say out loud. Do NOT invent facts, numbers, or "
                 "specifics to replace what you cut.")
    return "\n".join(lines) + "\n"
