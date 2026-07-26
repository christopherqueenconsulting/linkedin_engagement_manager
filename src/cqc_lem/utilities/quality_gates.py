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
GATE_MEETING_CTA = "meeting_cta"
GATE_FACT_GROUNDING = "fact_grounding"

GATE_LABELS = {
    GATE_AUTHENTICITY: "Authenticity",
    GATE_SIMILARITY: "Near-duplicate",
    GATE_FOCUS: "Off your focus topics",
    GATE_MISSING_ASSET: "Missing media",
    GATE_MEETING_CTA: "Meeting-ask CTA",
    GATE_FACT_GROUNDING: "Unverified specifics",
}

# The user-tunable thresholds behind these gates, in the units the SPA edits them in. Bounds are
# enforced at the API boundary AND in db.update_engagement_preferences (the V52 lesson: one bad
# value in the single-row upsert rolls back every section).
AUTHENTICITY_SCORE_MIN_BOUNDS = (0, 100)
SIMILARITY_MAX_PCT_BOUNDS = (10, 100)


def clamp_threshold(value: Any, low: int, high: int) -> Optional[int]:
    """Clamp a user-supplied threshold into [low, high]. None (and anything unparseable) stays None,
    which every reader treats as 'use the deploy default'."""
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
    held the post at PENDING — the others are advisory notes shown alongside them."""
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
                       matched_excerpt: Optional[str] = None) -> dict:
    """Deterministic near-duplicate check against the user's own recent posts."""
    excerpt = (matched_excerpt or "").strip().replace("\n", " ")
    if len(excerpt) > 160:
        excerpt = excerpt[:157].rstrip() + "…"
    return build_finding(
        GATE_SIMILARITY,
        explanation=(f"This draft overlaps {round(score * 100)}% with one of your recent posts — "
                     f"above your {round(threshold * 100)}% ceiling. Reposting the same take "
                     f"suppresses reach for both."),
        remediation=("Change the angle, not the words: pick a different example, argue the opposite "
                     "side, or move the post to a new sub-topic."),
        score=round(float(score), 4), threshold=round(float(threshold), 4),
        details=[f"Closest recent post: “{excerpt}”"] if excerpt else None)


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


def meeting_cta_finding(phrases: Optional[list] = None) -> dict:
    """The draft closes on a meeting ask — the CTA shape the 70/20/10 policy bans (issue #618).
    Held, not just flagged: a call-booking close is the single biggest reach penalty in 2026 and the
    fix is a one-line edit."""
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
    that honestly deferred them to placeholders the author still has to fill in."""
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


def parse_gate_findings(raw: Any) -> list[dict]:
    """Coerce a persisted `posts.gate_reason` value (JSON string, bytes, or already-decoded list)
    into a list of findings. Anything unusable reads as 'no findings' — a malformed reason must
    never break the review queue."""
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
