"""Structured demotion reasons + remediation for the post quality gates (issue #421).

A draft that a gate demotes APPROVED -> PENDING used to be opaque in the review queue: the user saw
a pending post with no reason, no score, and nothing to do about it. Every gate now records a
FINDING here — gate name, the score against the threshold that failed it, a plain-English
explanation, and the specific remediation — which is persisted on `posts.gate_reason` and rendered
in Content Studio.

Pure and dependency-free (no DB, no LLM, no env reads) so both the content pipeline and the API can
build/parse the same shape, and so the copy is unit-testable on its own.
"""

import json
from typing import Any, Optional

GATE_AUTHENTICITY = "authenticity"
GATE_SIMILARITY = "similarity"
GATE_FOCUS = "focus_alignment"
GATE_MISSING_ASSET = "missing_asset"
GATE_MALFORMED_ASSET = "malformed_asset"
GATE_MEETING_CTA = "meeting_cta"
GATE_FACT_GROUNDING = "fact_grounding"
GATE_SLOP = "ai_slop"
GATE_SLIDE_SLOP = "slide_ai_slop"
GATE_AFFILIATE_PROMO = "affiliate_promo"

GATE_LABELS = {
    GATE_AUTHENTICITY: "Authenticity",
    GATE_SIMILARITY: "Near-duplicate",
    GATE_FOCUS: "Off your focus topics",
    GATE_MISSING_ASSET: "Missing media",
    GATE_MALFORMED_ASSET: "Unusable media file",
    GATE_MEETING_CTA: "Meeting-ask CTA",
    GATE_FACT_GROUNDING: "Unverified specifics",
    GATE_SLOP: "AI-slop patterns",
    GATE_SLIDE_SLOP: "AI-slop patterns on the slides",
    GATE_AFFILIATE_PROMO: "Affiliate promotion",
}

# The user-tunable thresholds behind these gates, in the units the SPA edits them in. Bounds are
# enforced at the API boundary AND in db.update_engagement_preferences (the V52 lesson: one bad
# value in the single-row upsert rolls back every section).
AUTHENTICITY_SCORE_MIN_BOUNDS = (0, 100)
SIMILARITY_MAX_PCT_BOUNDS = (10, 100)

# Mirrors `content_framework.SIMILARITY_MEASURE_EMBEDDING` (issue #1265). Kept as a literal — the
# same reason that module mirrors the experiment plumbing's variant name — so this pure copy module
# never imports the content core; tests/unit/utilities/test_quality_gates.py pins the two together.
SIMILARITY_MEASURE_EMBEDDING = "embedding"


def clamp_threshold(value: Any, low: int, high: int) -> Optional[int]:
    """Clamp a user-supplied threshold into [low, high]. None (and anything unparseable) stays None,
    which every reader treats as 'use the deploy default'.
    """
    if value is None or value == "":
        return None
    try:
        return min(high, max(low, int(value)))
    except (TypeError, ValueError):
        return None


def build_finding(gate: str, explanation: str, remediation: str,
                  score: Optional[float] = None, threshold: Optional[float] = None,
                  demoted: bool = True, details: Optional[list] = None) -> dict:
    """One gate result in the shape the SPA renders. `demoted` marks the findings that actually
    held the post at PENDING — the others are advisory notes shown alongside them.
    """
    return {
        "gate": gate,
        "label": GATE_LABELS.get(gate, gate),
        "score": score,
        "threshold": threshold,
        "demoted": bool(demoted),
        "explanation": explanation,
        "remediation": remediation,
        "details": [str(d) for d in (details or []) if str(d).strip()],
    }


def authenticity_finding(score: int, threshold: int, reasons: Optional[list] = None) -> dict:
    """A1 authenticity judge scored the draft below the user's minimum (issue #382)."""
    return build_finding(
        GATE_AUTHENTICITY,
        explanation=(f"The authenticity check scored this draft {score} out of 100 — below your "
                     f"{threshold} minimum. LinkedIn's 2026 ranking demotes content that reads as "
                     f"generic AI, so it is held for your review instead of auto-scheduled."),
        remediation=("Raise the personal specificity: add a first-hand detail only you could write "
                     "(a real number, a client moment, what you got wrong), cut interchangeable "
                     "thought-leader phrasing, and make sure the take is clearly yours."),
        score=score, threshold=threshold, details=reasons)


def similarity_finding(score: float, threshold: float,
                       matched_excerpt: Optional[str] = None,
                       measure: Optional[str] = None) -> dict:
    """Near-duplicate check against the user's own recent posts.

    `measure` names WHICH of the two measures fired (issue #1265) — `embedding` cosine, the semantic
    one, or the deterministic `lexical` token overlap it degrades to when the embedding endpoint is
    unavailable. The two run on different scales, so a reader comparing this score with the nightly
    trend line has to be told which one produced it; an omitted measure keeps the original
    token-overlap wording, which is what every pre-#1265 caller meant.
    """
    excerpt = (matched_excerpt or "").strip().replace("\n", " ")
    if len(excerpt) > 160:
        excerpt = excerpt[:157].rstrip() + "…"
    semantic = measure == SIMILARITY_MEASURE_EMBEDDING
    if semantic:
        # "the", not "your": the cosine ceiling is a deploy-wide knob, while the token-overlap one
        # below really is the user's own Account setting.
        explanation = (f"This draft says the same thing as one of your recent posts — {round(score * 100)}% "
                       f"semantic match, above the {round(threshold * 100)}% ceiling. Rewording an "
                       f"earlier take suppresses reach for both.")
    else:
        explanation = (f"This draft overlaps {round(score * 100)}% with one of your recent posts — "
                       f"above your {round(threshold * 100)}% ceiling. Reposting the same take "
                       f"suppresses reach for both.")
    details = [f"Closest recent post: “{excerpt}”"] if excerpt else []
    if measure:
        details.append("Measured by " + ("embedding cosine (meaning, not wording)" if semantic
                                         else "token overlap (wording)"))
    return build_finding(
        GATE_SIMILARITY,
        explanation=explanation,
        remediation=("Change the angle, not the words: pick a different example, argue the opposite "
                     "side, or move the post to a new sub-topic."),
        score=round(float(score), 4), threshold=round(float(threshold), 4),
        details=details or None)


def focus_finding(score: float, threshold: float, topics: Optional[list] = None) -> dict:
    """Topic-authority (Topic DNA) governor — advisory: an off-niche post is flagged, never held."""
    topic_list = ", ".join([str(t).strip() for t in (topics or []) if str(t).strip()])
    return build_finding(
        GATE_FOCUS,
        explanation=(f"This draft scores {round(score * 100)}% against your declared focus topics "
                     f"(target {round(threshold * 100)}%). Posting off-niche dilutes the topic "
                     f"authority that drives your reach."),
        remediation=("Tie the post back to a focus topic explicitly, or add the topic to Content "
                     "Focus & Goals if this is a direction you now want to be known for."
                     + (f" Your focus topics: {topic_list}." if topic_list else "")),
        score=round(float(score), 4), threshold=round(float(threshold), 4), demoted=False)


def missing_asset_finding(post_type: str) -> dict:
    """A video/carousel post whose media never rendered — held so it can't publish assetless."""
    kind = "video" if str(post_type).lower() == "video" else "slides"
    return build_finding(
        GATE_MISSING_ASSET,
        explanation=(f"This {str(post_type).lower()} post has no {kind} yet, so it cannot be "
                     f"published as-is."),
        remediation=("Wait for the media backfill to finish, re-generate the post, or switch it to "
                     "a text post."),
        score=None, threshold=None)


def malformed_asset_finding(post_type: str, reason: str = "", demoted: bool = True) -> dict:
    """A video file was downloaded but is empty or unparseable — held so it can't publish broken.

    The `reason` is surfaced to the review UI so a user/dev sees whether the failure was a zero-byte
    file, a missing codec signature, or an ffprobe parse failure (issue #1280).

    `demoted=False` records the same reason as an ADVISORY note (issue #1402): a rejected file is
    never stored, so the missing-asset gate is already holding that post and this only explains WHY
    the media is missing. The probe pipeline demotes it only when `VIDEO_PROBE_ENABLED` makes a
    malformed asset a hard failure.
    """
    kind = "video" if str(post_type).lower() == "video" else "media file"
    return build_finding(
        GATE_MALFORMED_ASSET,
        explanation=(f"This {str(post_type).lower()} post's {kind} failed the probe: "
                     f"{reason or 'file is empty or unparseable'}."),
        remediation=("Wait for the media backfill to retry, re-generate the post, or replace the "
                     "video manually."),
        score=None, threshold=None, demoted=demoted,
        details=[reason] if reason else [])


def meeting_cta_finding(phrases: Optional[list] = None) -> dict:
    """The draft closes on a meeting ask — the CTA shape the 70/20/10 policy bans (issue #618).
    Held, not just flagged: a call-booking close is the single biggest reach penalty in 2026 and the
    fix is a one-line edit.
    """
    return build_finding(
        GATE_MEETING_CTA,
        explanation=("This draft asks the reader for a call or meeting. Salesy CTAs cost up to 70% of "
                     "a post's reach in 2026, so it is held instead of auto-scheduled."),
        remediation=("Swap the meeting ask for an ARTIFACT the reader gets without talking to anyone "
                     "— your lead-magnet resource (comment your trigger word) or your newsletter — or "
                     "close on a specific question instead."),
        score=None, threshold=None, details=phrases)


def fact_grounding_finding(unverified: Optional[list] = None,
                           placeholders: Optional[list] = None) -> dict:
    """No-fabrication guard for the save-targeted archetypes (issue #619 / G4). Two shapes, both
    holding the post: a draft that stated specifics nothing verifies (it made them up), and a draft
    that honestly deferred them to placeholders the author still has to fill in.
    """
    made_up = [str(u).strip() for u in (unverified or []) if str(u).strip()]
    to_fill = [str(p).strip() for p in (placeholders or []) if str(p).strip()]
    if made_up:
        return build_finding(
            GATE_FACT_GROUNDING,
            explanation=(f"This draft states {len(made_up)} specific(s) that no verified fact backs "
                         f"— on a build receipt or resource list those numbers ARE the post, so an "
                         f"invented one is the fastest way to lose the audience's trust."),
            remediation=("Replace each one with the real figure, or delete it. Adding the project to "
                         "your story bank lets future drafts use these numbers automatically."),
            details=[f"Unbacked specific: {m}" for m in made_up[:10]])
    return build_finding(
        GATE_FACT_GROUNDING,
        explanation=(f"This draft has {len(to_fill)} placeholder(s) waiting on your real numbers. "
                     f"It deliberately did not invent them, so it is held until you fill them in."),
        remediation="Edit the post, replace each [[...]] placeholder with the real detail, and re-score it.",
        details=[f"Fill in: {p}" for p in to_fill[:10]])


def slop_finding(hard_reasons: Optional[list] = None,
                 warn_reasons: Optional[list] = None) -> dict:
    """Deterministic AI-slop lint (issue #625 / D1). The draft still carries a pattern LinkedIn's
    2026 ranking suppresses after its regeneration budget ran out, so it is held with the exact
    constructions named — this gate is the only one that can tell the user *which sentence* to fix.
    """
    hard = [str(r).strip() for r in (hard_reasons or []) if str(r).strip()]
    warn = [str(r).strip() for r in (warn_reasons or []) if str(r).strip()]
    return build_finding(
        GATE_SLOP,
        explanation=(f"This draft still matches {len(hard)} AI-slop pattern(s) after being rewritten. "
                     f"LinkedIn's 2026 update suppresses these constructions outright, and readers "
                     f"skim past them, so it is held instead of auto-scheduled."),
        remediation=("Rewrite the flagged lines in the words you would actually say out loud — drop "
                     "the contrastive \"it's not X, it's Y\" framing, the manufactured \"here's the "
                     "kicker\" beats, the emoji bullets, and the reflex closer. Do not swap in "
                     "invented specifics for what you cut."),
        score=float(len(hard)), threshold=0.0,
        details=hard[:10] + [f"(advisory) {w}" for w in warn[:5]])


def slide_slop_finding(hard_reasons: Optional[list] = None,
                       warn_reasons: Optional[list] = None,
                       demoted: bool = False) -> dict:
    """The deterministic AI-slop lint (issue #625 / D1) read over a deck's SLIDE text (issue #1512).

    On a carousel the slides are what the reader reads, but only the caption has ever been linted,
    so a tier-1 tell pileup, a banned scaffold opener or a bait closer on a slide was recorded
    nowhere.

    ADVISORY by default, and that is a deliberate posture rather than a shortcut: slide text is baked
    into rendered images with no review queue, so a hold cannot be cleared by editing and re-scoring
    the way the caption's `ai_slop` hold can — the only remedy is regenerating the whole deck. The
    `demoted` flag exists so the holding posture is one argument away once it is decided; nothing
    passes it today.

    No `score`/`threshold`, unlike the caption's `slop_finding` — this is a count of patterns, not a
    measurement against a limit, and the SPA renders the pair as "score N · your limit M". The
    caption's finding is only ever built when a HARD check fired, so its count is always ≥ 1; a
    deck's is built on ANY violation, and on the `post` surface most of the checks that fire on the
    concatenated slide text are WARN-severity (burstiness, rule-of-three, canned scaffold), which
    would have rendered the dominant case as "score 0 · your limit 0" beside an explanation saying
    it matched a pattern. The count lives in the explanation and every reason in `details`, the same
    way the other advisory finding (`malformed_asset_finding`) carries its reading.
    """
    hard = [str(r).strip() for r in (hard_reasons or []) if str(r).strip()]
    warn = [str(r).strip() for r in (warn_reasons or []) if str(r).strip()]
    counted = len(hard) or len(warn)
    return build_finding(
        GATE_SLIDE_SLOP,
        explanation=(f"This deck's slide text matches {counted} AI-slop pattern(s). On a document "
                     f"post the slides are what the reader actually reads, and LinkedIn's 2026 "
                     f"update suppresses these constructions."
                     + ("" if demoted else " Recorded for your review — the post is not held on it.")),
        remediation=("Slide text is rendered into images, so it cannot be edited and re-scored: "
                     "regenerate the carousel if the flagged constructions matter to you."),
        score=None, threshold=None, demoted=demoted,
        details=hard[:10] + [f"(advisory) {w}" for w in warn[:5]])


def affiliate_promo_finding(disclosure: Optional[str] = None) -> dict:
    """Affiliate promotion published from the author's own account (issue #770).

    This is the one gate that is not a quality verdict — the draft may be perfect and it is still
    held. Consent to the program is a standing yes to LEM WRITING promotion; it is not a yes to any
    particular sentence going out over the author's name, and an endorsement is the one post type
    where "I never saw it before it published" is the outcome that costs them something. So every
    affiliate post waits for an explicit approval, and re-scoring cannot clear it: only the author
    pressing approve (or deleting the referral link) can.
    """
    return build_finding(
        GATE_AFFILIATE_PROMO,
        explanation=("This post promotes LinkedIn Engagement Manager and carries your referral link, "
                     "so it is a paid endorsement published under your name. Affiliate posts are "
                     "never auto-scheduled — this one is waiting for you to read it and approve it."),
        remediation=("Read it as your audience will. Approve it to schedule it, edit anything that "
                     "does not sound like you, or delete the referral link and the disclosure line "
                     "to turn it back into an ordinary post."),
        details=[d for d in [str(disclosure or "").strip()] if d])


def parse_gate_findings(raw: Any) -> list[dict]:
    """Coerce a persisted `posts.gate_reason` value (JSON string, bytes, or already-decoded list)
    into a list of findings. Anything unusable reads as 'no findings' — a malformed reason must
    never break the review queue.
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [f for f in raw if isinstance(f, dict) and f.get("gate")]


def demoting_findings(findings: Optional[list[dict]]) -> list[dict]:
    """The findings that hold a post at PENDING (vs. the advisory notes)."""
    return [f for f in (findings or []) if isinstance(f, dict) and f.get("demoted")]
