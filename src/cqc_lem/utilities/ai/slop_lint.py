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

import functools
import os
import re
import statistics
from typing import Optional

from cqc_lem.utilities.ai.content_alignment import AI_TELL_WORDS
from cqc_lem.utilities.ai.content_framework import (
    MOTION_BANNED_AUDIO,
    MOTION_BANNED_MONTAGE,
    MOTION_BANNED_MOOD,
    MOTION_OPENING_SIGNALS,
    NEWSLETTER_BANNED_SCAFFOLDS,
    POST_BANNED_SCAFFOLDS,
    content_tokens,
    text_similarity,
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
CHECK_BLOG_ALIGNMENT = "blog_alignment"

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
    CHECK_BLOG_ALIGNMENT: SEVERITY_WARN,
}

# Per-SURFACE severity, applied on top of DEFAULT_SEVERITIES (issue #1285). A check's false-positive
# risk is a property of the surface, not only of the check: `canned_scaffold` on a POST can catch a
# templated opener that carries a real specific, but a NEWSLETTER scaffold ("in today's edition",
# "without further ado") is pure runway — it says nothing, it is what the model reaches for when the
# edition has no first line, and there is no legitimate draft that needs it. Newsletters are also the
# one surface where a HARD verdict costs nothing a human notices: an edition is drafted for review
# days ahead of its slot, so HARD buys the bounded regeneration (`slop_retry_directive` steers on
# HARD violations only) and the reasons in the log, and never blocks a publish.
SURFACE_SEVERITIES: dict = {
    "newsletter": {CHECK_SCAFFOLD: SEVERITY_HARD},
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

# Token-overlap floor between a blog-aligned newsletter source and the generated edition body.
# A faithful repurposing should share a meaningful vocabulary footprint; a drift into generic
# advice falls below this. Kept WARN by default until calibrated against real blog-aligned editions.
BLOG_ALIGNMENT_MIN_DEFAULT = 0.15

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


def check_severity(check: str, content_type: Optional[str] = None) -> str:
    """This check's severity on `content_type`: 'hard' (regenerate, then block), 'warn' (report
    only), or 'off'.

    Resolved most-specific-first, so ops can always overrule a built-in:
    `SLOP_LINT_SEVERITY_<CHECK>_<SURFACE>` (e.g. SLOP_LINT_SEVERITY_CANNED_SCAFFOLD_NEWSLETTER=warn)
    beats the surface-wide `SLOP_LINT_SEVERITY_<CHECK>`, which beats `SURFACE_SEVERITIES` and then
    `DEFAULT_SEVERITIES`. An explicit env value wins over a per-surface DEFAULT on purpose — an ops
    instruction is newer evidence than a calibration baked into the code.
    """
    surface = str(content_type or "").strip().lower()
    names = ([f"SLOP_LINT_SEVERITY_{check.upper()}_{surface.upper()}"] if surface else [])
    names.append(f"SLOP_LINT_SEVERITY_{check.upper()}")
    for name in names:
        raw = (os.environ.get(name) or "").strip().lower()
        if raw in (SEVERITY_HARD, SEVERITY_WARN, SEVERITY_OFF):
            return raw
    per_surface = SURFACE_SEVERITIES.get(surface) or {}
    if check in per_surface:
        return per_surface[check]
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


def blog_alignment_min() -> float:
    """Minimum token overlap a blog-aligned newsletter edition must carry vs its source.

    `SLOP_LINT_BLOG_ALIGNMENT_MIN`, re-read on every call. A floor: scores below it fire the check.
    Range 0.0-1.0.
    """
    return float(_env_number("SLOP_LINT_BLOG_ALIGNMENT_MIN", BLOG_ALIGNMENT_MIN_DEFAULT, float,
                             low=0.0, high=1.0))


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


def _check_blog_alignment(text: str, sents: list, ctx: dict) -> Optional[dict]:
    """Newsletter-only check: the generated body must still vocabularly track the blog source.

    Compares meaningful-token overlap between the source material `blog_content` and the finished
    edition body. A low score means the edition drifted into generic advice and no longer repurposes
    the author's own writing. Skipped when there is no source (the toggle was off or unresolved) or
    on any surface other than newsletter.
    """
    if ctx.get("content_type") != "newsletter":
        return None
    blog = str(ctx.get("blog_content") or "").strip()
    if not blog:
        return None
    # A very short source makes the overlap signal noisy; require at least a handful of meaningful
    # tokens on both sides before grading.
    if len(content_tokens(blog)) < 5 or len(content_tokens(text)) < 5:
        return None
    score = text_similarity(blog, text)
    floor = blog_alignment_min()
    if score >= floor:
        return None
    return {"detail": (f"blog-alignment token overlap {score:.2f} is below the {floor:.2f} "
                       "floor — the edition may have drifted from the source material"),
            "evidence": [], "score": round(score, 4), "threshold": round(floor, 4)}


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
    (CHECK_BLOG_ALIGNMENT, _check_blog_alignment),
)


def lint_report(text: Optional[str], content_type: str = "post",
                exempt_keyword: Optional[str] = None,
                blog_content: Optional[str] = None) -> dict:
    """Grade one finished draft against every slop check. Deterministic, no LLM, no I/O — the same
    draft always gets the same verdict.

    Returns {passes, violations, hard, warnings, reasons, checked}. `passes` is False only when a
    HARD-severity check fired; `warnings` are recorded for the review UI and the logs but never hold
    a draft. Severity is resolved PER SURFACE (`check_severity`), so the same violation can be a
    warning on a post and hard on a newsletter. `checked` is False when the linter is disabled for
    this surface (or the text is empty), in which case the report is empty and passing — this layer
    fails OPEN, always.

    `exempt_keyword` is the user's lead-magnet trigger word: a "Comment YES" CTA is sanctioned, not
    bait, exactly as `strip_engagement_bait` treats it. `blog_content` is the newsletter source
    material for the optional blog-alignment fidelity gate (newsletter-only, skipped when absent).
    """
    empty = {"passes": True, "violations": [], "hard": [], "warnings": [], "reasons": [],
             "checked": False}
    if not text or not str(text).strip():
        return empty
    if not slop_lint_enabled(content_type):
        return empty

    body = str(text)
    sents = sentences(body)
    ctx = {"exempt_keyword": exempt_keyword, "content_type": content_type,
           "blog_content": blog_content}
    violations = []
    for name, fn in _CHECKS:
        severity = check_severity(name, content_type)
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


# ---------------------------------------------------------------------------
# Motion-prompt lint (issue #1277) — the CHECKING half of the #1140 writer contract.
#
# A finished motion prompt is graded here before `create_runway_video` spends a Runway credit, the
# same way `lint_report` grades a finished post. It reads the SAME `MOTION_BANNED_*` tuples
# `content_framework.motion_prompt_directive()` hands the writer, so a pattern cannot be banned in
# the prompt and unchecked here. Still pure regex/string — no LLM, no network, no DB.
#
# The severities below are the checker's OPINION; whether a hard violation actually holds a render
# is the caller's decision, gated on the `video-motion-lint-hold` flag (WARN-only by default). That
# split is deliberate: promoting this to a spend gate changes the credit profile, so it must be one
# runtime flip rather than a deploy.
# ---------------------------------------------------------------------------

MOTION_CHECK_MONTAGE = "motion_montage"
MOTION_CHECK_MOOD = "motion_mood"
MOTION_CHECK_AUDIO = "motion_audio"
MOTION_CHECK_OPENING = "motion_opening"

MOTION_VERDICT_UNCHECKED = "unchecked"
MOTION_VERDICT_PASS = "pass"
MOTION_VERDICT_WARN = "warn"
MOTION_VERDICT_REGENERATE = "regenerate"
MOTION_VERDICT_HOLD = "hold"

# Three of the four are HARD because every entry in their list is a pattern Gen-4 cannot render as
# asked (a cut, a film stock, a voiceover) — there is no good prompt that carries one. The opening
# check is WARN because it is an ALLOW-list heuristic: a legitimate prompt can open on a subject
# beat this list has no verb for, and grading that as a spend gate would refuse good renders.
MOTION_DEFAULT_SEVERITIES: dict = {
    MOTION_CHECK_MONTAGE: SEVERITY_HARD,
    MOTION_CHECK_MOOD: SEVERITY_HARD,
    MOTION_CHECK_AUDIO: SEVERITY_HARD,
    MOTION_CHECK_OPENING: SEVERITY_WARN,
}
DEFAULT_SEVERITIES.update(MOTION_DEFAULT_SEVERITIES)

# Prompts one video may spend when enforcement is ON (initial + regenerations). Two on purpose:
# each regeneration is a `lem-simple` call, and a prompt that still carries a banned pattern after
# one steered rewrite is not one more retry away from clean.
MOTION_MAX_ATTEMPTS_DEFAULT = 2


class MotionPromptHeld(Exception):
    """Raised when an enforced motion prompt still violates the contract after its last attempt.

    Carries the report so the caller can log WHICH checks held the render rather than a bare
    failure — the video path treats this like any other generation failure (refund, fall back).
    """

    def __init__(self, message: str, report: Optional[dict] = None):
        """Hold `report` alongside the message so the caller can log which checks held it."""
        super().__init__(message)
        self.report = dict(report or {})


@functools.lru_cache(maxsize=256)
def _motion_phrase_re(phrase: str, suffix_tolerant: bool = False) -> "re.Pattern":
    """Word-boundary matcher for one banned/allowed phrase, whitespace-flexible.

    Boundaries matter here: "4k" must not fire inside a longer token, and a prompt that wraps
    "then transition to" across a line break is the same violation as one that does not.
    """
    body = r"\s+".join(re.escape(part) for part in phrase.split())
    tail = r"(?:s|es|ing|ed|d)?" if suffix_tolerant else ""
    return re.compile(rf"(?<![\w-]){body}{tail}(?![\w-])", re.IGNORECASE)


def motion_lint_enabled() -> bool:
    """Whether motion prompts are graded at all (`MOTION_PROMPT_LINT_ENABLED`, default ON).

    Grading is free and, until the hold flag is flipped, changes nothing but telemetry — so this
    exists as the kill switch for the telemetry itself, not as the enforcement control.
    """
    return _env_flag("MOTION_PROMPT_LINT_ENABLED", True)


def motion_max_attempts() -> int:
    """Prompts one video may spend under enforcement (`MOTION_PROMPT_LINT_MAX_ATTEMPTS`)."""
    return int(_env_number("MOTION_PROMPT_LINT_MAX_ATTEMPTS", MOTION_MAX_ATTEMPTS_DEFAULT, int,
                           low=1, high=4))


def motion_half(prompt: Optional[str]) -> str:
    """The prompt WITHOUT the deterministic audio clause `_audio_direction()` appends.

    Every check grades this half only. The appended clause legitimately says "no voiceover, no
    narration" (issue #548), so grading the whole string would make the fix for one defect fire the
    audio check on every audio-capable render.

    Only LEM's OWN clause is cut, matched on `AUDIO_DIRECTION_MARKER` + `AUDIO_DIRECTION_LEAD`
    together: a writer that ignores the contract answers with the same "Audio:" marker, so cutting
    on the marker alone would hide the one violation the audio check exists to catch.
    """
    from cqc_lem.utilities.ai.video_models import AUDIO_DIRECTION_LEAD, AUDIO_DIRECTION_MARKER
    pattern = re.compile(rf"{re.escape(AUDIO_DIRECTION_MARKER)}\s*{re.escape(AUDIO_DIRECTION_LEAD)}.*$",
                         re.IGNORECASE | re.DOTALL)
    return pattern.sub("", str(prompt or "")).strip()


def find_motion_montage(prompt: Optional[str]) -> list:
    """Montage/edit language in the motion half, in list order, deduped."""
    text = motion_half(prompt)
    return [p for p in MOTION_BANNED_MONTAGE if _motion_phrase_re(p).search(text)]


def find_motion_mood(prompt: Optional[str]) -> list:
    """Mood / film-stock / render-quality adjectives in the motion half."""
    text = motion_half(prompt)
    return [p for p in MOTION_BANNED_MOOD if _motion_phrase_re(p).search(text)]


def find_motion_audio(prompt: Optional[str]) -> list:
    """Audio language the WRITER added, on any model — the deterministic clause is excluded."""
    text = motion_half(prompt)
    return [p for p in MOTION_BANNED_AUDIO if _motion_phrase_re(p).search(text)]


def has_opening_signal(prompt: Optional[str]) -> bool:
    """Whether the FIRST sentence names a camera move, a subject motion, or an immediacy cue.

    Only the first sentence counts: LinkedIn autoplays muted and the viewer is gone by second 3, so
    a push-in that is only mentioned in the closing sentence is not an opening.
    """
    sents = sentences(motion_half(prompt))
    if not sents:
        return False
    first = sents[0]
    return any(_motion_phrase_re(p, True).search(first) for p in MOTION_OPENING_SIGNALS)


def _check_motion_montage(text: str) -> Optional[dict]:
    hits = find_motion_montage(text)
    if not hits:
        return None
    return {"detail": ("asks for an edited sequence Gen-4 cannot render as a single shot: "
                       + ", ".join(f'"{h}"' for h in hits[:5])),
            "evidence": hits[:5], "score": float(len(hits)), "threshold": 0.0}


def _check_motion_mood(text: str) -> Optional[dict]:
    hits = find_motion_mood(text)
    if not hits:
        return None
    return {"detail": ("keyword-stuffs mood/film-stock adjectives instead of describing motion: "
                       + ", ".join(f'"{h}"' for h in hits[:5])),
            "evidence": hits[:5], "score": float(len(hits)), "threshold": 0.0}


def _check_motion_audio(text: str) -> Optional[dict]:
    hits = find_motion_audio(text)
    if not hits:
        return None
    return {"detail": ("describes audio, which the appended audio direction owns — a self-authored "
                       "one invents voiceovers on audio-capable models: "
                       + ", ".join(f'"{h}"' for h in hits[:5])),
            "evidence": hits[:5], "score": float(len(hits)), "threshold": 0.0}


def _check_motion_opening(text: str) -> Optional[dict]:
    if has_opening_signal(text):
        return None
    first = (sentences(motion_half(text)) or [""])[0]
    # `evidence` stays EMPTY on purpose: it is the one field `track_motion_prompt_check` puts on
    # the event, and this check's only evidence is prompt text — which can echo the user's post.
    # The excerpt lives in `detail`, which reaches the logs and the retry steer, never PostHog.
    return {"detail": ("names no camera move or subject motion in its opening sentence "
                       f"(\"{_excerpt(first, 80)}\") — nothing is readable inside the muted "
                       "autoplay window"),
            "evidence": [], "score": 0.0, "threshold": 1.0}


_MOTION_CHECKS: tuple = (
    (MOTION_CHECK_MONTAGE, _check_motion_montage),
    (MOTION_CHECK_MOOD, _check_motion_mood),
    (MOTION_CHECK_AUDIO, _check_motion_audio),
    (MOTION_CHECK_OPENING, _check_motion_opening),
)


def motion_prompt_report(prompt: Optional[str], model: Optional[str] = None) -> dict:
    """Grade one FINISHED motion prompt against the #1140 contract. Deterministic, no LLM, no I/O.

    Returns {passes, violations, hard, warnings, reasons, checks, checked, chars, model}. `passes`
    is False only when a HARD check fired; `checked` is False when the linter is disabled or the
    prompt is empty, in which case the report is empty and passing — this layer fails OPEN, exactly
    like `lint_report`.

    `model` is recorded for telemetry only. The audio check runs on every model: the contract tells
    the writer to say nothing about audio at all, and a silent model simply ignores the words it
    was charged to generate.
    """
    empty = {"passes": True, "violations": [], "hard": [], "warnings": [], "reasons": [],
             "checks": [], "checked": False, "chars": 0, "model": model}
    if not prompt or not str(prompt).strip():
        return empty
    if not motion_lint_enabled():
        return empty

    body = motion_half(prompt)
    violations = []
    for name, fn in _MOTION_CHECKS:
        severity = check_severity(name)
        if severity == SEVERITY_OFF:
            continue
        found = fn(prompt)
        if not found:
            continue
        violations.append({"check": name, "severity": severity, **found})

    hard = [v for v in violations if v["severity"] == SEVERITY_HARD]
    warnings = [v for v in violations if v["severity"] == SEVERITY_WARN]
    return {"passes": not hard, "violations": violations, "hard": hard, "warnings": warnings,
            "reasons": [f"{v['check']}: {v['detail']}" for v in violations],
            "checks": [v["check"] for v in violations], "checked": True,
            "chars": len(body), "model": model}


def motion_prompt_verdict(report: Optional[dict], enforced: bool = False, attempt: int = 1,
                          max_attempts: Optional[int] = None) -> str:
    """What to DO about a graded prompt: unchecked, pass, warn, regenerate, or hold.

    `enforced` is the `video-motion-lint-hold` flag, read by the caller — with it off (the default)
    a hard violation still only reports, so nothing about the credit-spend profile changes until
    the flag is flipped. With it on, a hard violation buys one steered rewrite per remaining
    attempt and holds the render on the last one. WARN-severity findings never regenerate and never
    hold, however many there are.
    """
    report = dict(report or {})
    if not report.get("checked"):
        return MOTION_VERDICT_UNCHECKED
    if not report.get("violations"):
        return MOTION_VERDICT_PASS
    if not report.get("hard") or not enforced:
        return MOTION_VERDICT_WARN
    ceiling = motion_max_attempts() if max_attempts is None else max(1, int(max_attempts))
    return MOTION_VERDICT_REGENERATE if attempt < ceiling else MOTION_VERDICT_HOLD


def motion_retry_directive(violations: Optional[list]) -> str:
    """The regeneration steer after a motion prompt fails the lint.

    Names each pattern that fired, so the rewrite fixes the construction instead of paraphrasing
    around it.
    """
    reasons = [v for v in (violations or []) if isinstance(v, dict) and v.get("detail")]
    if not reasons:
        return ""
    lines = ["\n\nYOUR PREVIOUS MOTION PROMPT VIOLATED THE CONTRACT. Rewrite it so none of these "
             "remain — describe the SAME shot, change only how you describe it:"]
    for v in reasons:
        lines.append(f"- It {v['detail']}.")
    lines.append("- One continuous camera move plus what the subject physically does, opening in "
                 "the first second. Nothing about mood, film stock, edits or audio.")
    return "\n".join(lines) + "\n"
