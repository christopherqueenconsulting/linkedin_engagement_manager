"""Lead scoring for the CRM-lite pipeline (issue #484).

Pure scoring math — no DB, no I/O — so the ranking that decides who the owner talks to today is
deterministic and cheap to test. `run_scheduler._rebuild_leads_for_user` supplies the activity rows
(read from data the automation already records) and persists what comes back.

The score is ICP fit weighted by engagement recency and frequency: engagement decides HOW MUCH they
care, ICP fit decides how much that is worth. Neither alone is a lead.
"""

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from cqc_lem.utilities.db import LeadSignalKind, LeadStage

# Base points per signal, before recency/frequency weighting. An inbound buying question dwarfs
# everything else — it is the only signal where THEY started the sales conversation.
SIGNAL_POINTS: dict = {
    LeadSignalKind.INTENT: 55,
    LeadSignalKind.PROFILE_VIEW: 14,
    LeadSignalKind.FUNNEL: 12,
    LeadSignalKind.DM: 10,
    LeadSignalKind.CONNECT: 8,
    LeadSignalKind.ENGAGED: 7,
}

# Engagement decays on a half-life rather than a cliff: a comment 14 days ago is worth half a
# comment today, and nothing ever decays to zero (floor) so old-but-real signal still ranks.
RECENCY_HALF_LIFE_DAYS = 14.0
RECENCY_FLOOR = 0.1

# Stage thresholds on the blended 0-100 score.
HOT_SCORE = 70
WARM_SCORE = 35

# ICP fit we report when we have no profile facts for someone: neutral, never a penalty. Most
# engagers are never scraped, and guessing them cold would bury real leads.
ICP_UNKNOWN = 50

# Title seniority — a buying-authority proxy. Matched whole-word (see _mentions).
_SENIOR_TITLES = ("chief", "founder", "co-founder", "owner", "ceo", "cto", "coo", "cmo", "cfo",
                  "president", "partner", "vp", "vice president", "head of")
_MID_TITLES = ("director", "manager", "principal", "lead")

_STAGE_ACTIONS: dict = {
    LeadStage.OPPORTUNITY: "Answer their question, then offer a specific time to talk",
    LeadStage.IN_CONVERSATION: "Keep the thread moving — reply with something useful to them",
    LeadStage.HOT: "Reach out personally today while the interest is fresh",
    LeadStage.WARM: "Comment on their next post, then send a connection note",
    LeadStage.COLD: "Leave in the nurture rotation — react to and comment on their posts",
}


@dataclass
class LeadActivity:
    """One engagement signal about one person, as read from an existing table."""
    kind: str
    occurred_at: Optional[datetime] = None
    detail: str = ""          # source-specific state (e.g. the lead_signals status, funnel stage)


@dataclass
class ScoredLead:
    """A person plus everything the pipeline board and the nightly upsert need."""
    person_key: str
    person_name: Optional[str] = None
    person_profile_url: Optional[str] = None
    score: int = 0
    icp_score: int = ICP_UNKNOWN
    engagement_score: int = 0
    stage: str = LeadStage.COLD
    signals: tuple = ()
    signal_count: int = 0
    reasons: str = ""
    next_action: str = ""
    first_signal_at: Optional[datetime] = None
    last_signal_at: Optional[datetime] = None
    activities: list = field(default_factory=list)


def profile_slug(profile_url: str) -> str:
    """The /in/<slug> identity from a profile URL — the stable half of a person's key."""
    m = re.search(r"/in/([^/?#]+)", profile_url or "")
    return m.group(1).lower() if m else ""


def person_key(person_name: str = None, person_profile_url: str = None) -> str:
    """Stable per-person identity so the same human found via post_engagers, a DM, and a lead signal
    collapses to ONE lead row. Prefers the profile slug; falls back to the normalized name (all we
    get from feed/comment scrapes). Empty when we have neither — such a row is skipped."""
    slug = profile_slug(person_profile_url)
    if slug:
        return f"in:{slug}"
    name = re.sub(r"\s+", " ", (person_name or "")).strip().lower()
    return f"name:{name}" if name else ""


def _age_days(occurred_at: datetime, now: datetime) -> float:
    """Days between two timestamps, tolerating one side being tz-aware: MySQL hands back naive-UTC
    datetimes while callers may pass an aware `now`, and mixing them raises."""
    if (occurred_at.tzinfo is None) != (now.tzinfo is None):
        occurred_at = occurred_at.replace(tzinfo=now.tzinfo)
    return (now - occurred_at).total_seconds() / 86400.0


def recency_weight(occurred_at: Optional[datetime], now: datetime) -> float:
    """Half-life decay in [RECENCY_FLOOR, 1.0]. An unknown or future timestamp is treated as now —
    a missing date should never silently zero out a real signal."""
    if not occurred_at:
        return 1.0
    days = _age_days(occurred_at, now)
    if days <= 0:
        return 1.0
    return max(RECENCY_FLOOR, 0.5 ** (days / RECENCY_HALF_LIFE_DAYS))


def _mentions(text: str, term: str) -> bool:
    """Whole-word match. Substring matching is wrong here in both directions: 'cto' is inside
    'director' and 'ai' is inside 'chain', and either one silently mis-scores a lead."""
    return bool(re.search(rf"\b{re.escape(term)}\b", text))


def icp_fit(facts: dict, target_terms: Iterable[str]) -> int:
    """0-100 fit of a person's title/company/industry against what the user says they care about
    (focus topics, include topics/keywords). ICP_UNKNOWN when we have no facts on them."""
    blob = " ".join(str(facts.get(k) or "") for k in ("job_title", "company_name", "industry")).lower()
    if not blob.strip():
        return ICP_UNKNOWN

    terms = {str(t).strip().lower() for t in (target_terms or []) if str(t or "").strip()}
    if terms:
        hits = sum(1 for t in terms if _mentions(blob, t))
        # Matching three of the user's terms is already a clear fit; more shouldn't keep paying.
        # A user who listed fewer than three saturates on matching all of them.
        topic_score = 65 * min(1.0, hits / min(3.0, len(terms)))
    else:
        # No stated targeting to match against — don't pretend to know, stay neutral on this half.
        topic_score = 65 * 0.5

    title = str(facts.get("job_title") or "").lower()
    if any(_mentions(title, t) for t in _SENIOR_TITLES):
        seniority = 35
    elif any(_mentions(title, t) for t in _MID_TITLES):
        seniority = 20
    else:
        seniority = 5
    return max(0, min(100, round(topic_score + seniority)))


def engagement_strength(activities: Iterable[LeadActivity], now: datetime) -> int:
    """0-100 from recency AND frequency. Repeats of the same signal add with diminishing returns
    (1, 1/2, 1/3 …) so one person commenting ten times never outranks someone who asked to buy."""
    by_kind: dict = {}
    for a in activities or []:
        by_kind.setdefault(a.kind, []).append(a)

    total = 0.0
    for kind, items in by_kind.items():
        base = SIGNAL_POINTS.get(kind, 5)
        items = sorted(items, key=lambda a: (a.occurred_at is not None, a.occurred_at), reverse=True)
        for i, a in enumerate(items):
            total += base * recency_weight(a.occurred_at, now) / (i + 1)
    return max(0, min(100, round(total)))


def _has(activities: Iterable[LeadActivity], kind: str) -> bool:
    return any(a.kind == kind for a in activities or [])


def derive_stage(activities: Iterable[LeadActivity], score: int) -> str:
    """Where this person sits in the funnel. Ordered warmest-first: the first rule that matches wins."""
    acts = list(activities or [])
    answered_intent = any(a.kind == LeadSignalKind.INTENT and a.detail in ("approved", "sent")
                          for a in acts)
    if answered_intent:
        return LeadStage.OPPORTUNITY
    # A DM thread only counts as a conversation when they also engage with us — an unanswered
    # outbound DM is not a relationship.
    inbound = _has(acts, LeadSignalKind.ENGAGED) or _has(acts, LeadSignalKind.PROFILE_VIEW) \
        or _has(acts, LeadSignalKind.INTENT)
    if _has(acts, LeadSignalKind.DM) and inbound:
        return LeadStage.IN_CONVERSATION
    if _has(acts, LeadSignalKind.INTENT) or score >= HOT_SCORE:
        return LeadStage.HOT
    if score >= WARM_SCORE:
        return LeadStage.WARM
    return LeadStage.COLD


def recommend_action(stage: str, activities: Iterable[LeadActivity]) -> str:
    """The one next move for this lead. Never recommends something we've already done to them."""
    acts = list(activities or [])
    if stage == LeadStage.HOT and _has(acts, LeadSignalKind.INTENT):
        return "Reply to their question in the Leads inbox, then offer a call"
    if stage == LeadStage.HOT and _has(acts, LeadSignalKind.CONNECT):
        return "Send a personal DM — they're already connected and engaging hard"
    if stage == LeadStage.WARM and _has(acts, LeadSignalKind.CONNECT):
        return "Comment on their next post — the invite is already out"
    return _STAGE_ACTIONS.get(stage, _STAGE_ACTIONS[LeadStage.COLD])


def _describe(activities: list, icp: int, now: datetime) -> str:
    """Plain-language WHY, ordered by how much each signal actually moved the score."""
    counts: dict = {}
    for a in activities:
        counts[a.kind] = counts.get(a.kind, 0) + 1
    labels = {
        LeadSignalKind.INTENT: "asked a buying question",
        LeadSignalKind.PROFILE_VIEW: "viewed your profile",
        LeadSignalKind.ENGAGED: "engaged with your posts",
        LeadSignalKind.DM: "in a DM thread with you",
        LeadSignalKind.CONNECT: "you sent a connection request",
        LeadSignalKind.FUNNEL: "in your outreach funnel",
    }
    parts = []
    for kind, _ in sorted(counts.items(), key=lambda kv: SIGNAL_POINTS.get(kv[0], 0), reverse=True):
        label = labels.get(kind, str(kind))
        parts.append(f"{label} ({counts[kind]}×)" if counts[kind] > 1 else label)

    last = max((a.occurred_at for a in activities if a.occurred_at), default=None)
    if last:
        days = max(0, math.floor(_age_days(last, now)))
        parts.append("last signal today" if days == 0 else f"last signal {days}d ago")
    if icp >= 70:
        parts.append("strong ICP fit")
    elif icp != ICP_UNKNOWN and icp < 40:
        parts.append("weak ICP fit")
    return " · ".join(parts)[:512]


def score_lead(activities: list, now: datetime, person_name: str = None,
               person_profile_url: str = None, facts: dict = None,
               target_terms: Iterable[str] = None) -> ScoredLead:
    """Score ONE person. The blend keeps engagement dominant (it is observed behaviour) while ICP
    fit scales it up or down by up to ±40% — an unknown-fit person lands right where engagement
    alone puts them."""
    acts = list(activities or [])
    icp = icp_fit(facts or {}, target_terms or [])
    engagement = engagement_strength(acts, now)
    multiplier = 0.6 + 0.8 * (icp / 100.0)
    score = max(0, min(100, round(engagement * multiplier)))
    stage = derive_stage(acts, score)
    stamps = [a.occurred_at for a in acts if a.occurred_at]
    return ScoredLead(
        person_key=person_key(person_name, person_profile_url),
        person_name=person_name,
        person_profile_url=person_profile_url,
        score=score,
        icp_score=icp,
        engagement_score=engagement,
        stage=stage,
        signals=tuple(sorted({a.kind for a in acts})),
        signal_count=len(acts),
        reasons=_describe(acts, icp, now),
        next_action=recommend_action(stage, acts),
        first_signal_at=min(stamps) if stamps else None,
        last_signal_at=max(stamps) if stamps else None,
        activities=acts,
    )


def group_activity(rows: Iterable[dict]) -> dict:
    """Collapse raw activity rows (from db.get_lead_activity) into {person_key: {...}}. Rows for the
    same human arriving under different sources merge here — the profile URL from ANY row becomes
    the lead's URL, so a name-only comment and a DM to the same person stay one lead."""
    people: dict = {}
    for row in rows or []:
        name = (row.get("person_name") or "").strip() or None
        url = (row.get("person_profile_url") or "").strip() or None
        key = person_key(name, url)
        if not key:
            continue
        person = people.setdefault(key, {"person_key": key, "person_name": name,
                                         "person_profile_url": url, "activities": []})
        person["person_name"] = person["person_name"] or name
        person["person_profile_url"] = person["person_profile_url"] or url
        person["activities"].append(LeadActivity(kind=row.get("kind"),
                                                 occurred_at=row.get("occurred_at"),
                                                 detail=str(row.get("detail") or "")))
    return people


def score_leads(rows: Iterable[dict], now: datetime, facts_by_url: dict = None,
                target_terms: Iterable[str] = None) -> list:
    """Score every person in a user's activity rows, hottest first."""
    facts_by_url = {profile_slug(k): v for k, v in (facts_by_url or {}).items() if profile_slug(k)}
    leads = []
    for person in group_activity(rows).values():
        facts = facts_by_url.get(profile_slug(person["person_profile_url"]), {})
        leads.append(score_lead(person["activities"], now,
                                person_name=person["person_name"],
                                person_profile_url=person["person_profile_url"],
                                facts=facts, target_terms=target_terms))
    leads.sort(key=lambda l: (l.score, l.signal_count), reverse=True)
    return leads
