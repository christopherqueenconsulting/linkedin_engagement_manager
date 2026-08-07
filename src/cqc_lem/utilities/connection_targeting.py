"""Smart connection targeting from content engagers (issue #486, extends #398).

Pure ranking logic — no DB, no Selenium, no LLM — so WHO we ask to connect with is deterministic and
cheap to test. The sourcing task (`run_automation.scan_connection_candidates`) supplies the raw
signals it read/scraped and files what comes back as #398 connection requests.

The premise: someone who just commented on our post (or on an adjacent thought-leader's post about
our topic) is a warm, on-topic 2nd-degree prospect. ICP fit decides whether they're worth a request
at all; warmth (how recently/often they engaged, and whose content they engaged with) decides the
order we work through them.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from cqc_lem.utilities.lead_scoring import ICP_UNKNOWN, icp_fit, person_key, profile_slug, recency_weight

# How they showed up on our radar. Engaging with OUR content is a stronger statement of interest in
# US than engaging with someone else's post about the same topic.
SOURCE_OWN_POST = "own_post"
SOURCE_ADJACENT_POST = "adjacent_post"
# The roster ladder (issue #979): a curated account whose posts we read and comment on, where
# following did not unlock commenting. Never SOURCED here — the roster is the user's own list, so
# this source only ever names the note's shared ground; it carries no warmth weight.
SOURCE_ROSTER = "roster"
SOURCE_WEIGHTS: dict = {SOURCE_OWN_POST: 1.0, SOURCE_ADJACENT_POST: 0.65}

# Points one engagement is worth before recency/source weighting. Warmth saturates at 100, so a
# single fresh engagement on our own post already lands mid-range and repeats push it up.
SIGNAL_POINTS = 45

# Blend. Fit is the gate (see rank_candidates' floor), warmth is the ranking — but fit still carries
# the larger share so a high-volume engager with no fit can't outrank a real prospect.
ICP_WEIGHT = 0.6
WARMTH_WEIGHT = 0.4

# Default ICP floor for people we have facts on. Mirrors the DB default (min_connection_icp_score).
MIN_ICP_DEFAULT = 55

# Connection-degree badges that make a connection request pointless: we are already connected, so
# LinkedIn shows Message where Connect would be and the invite can only fail (issue #623).
EXCLUDED_DEGREES = ("1st",)

# LinkedIn caps a connection-request note at 300 characters.
CONNECT_NOTE_LIMIT = 300


@dataclass
class CandidateSignal:
    """One observed engagement by one person, from one source."""
    person_name: Optional[str] = None
    person_profile_url: Optional[str] = None
    source: str = SOURCE_OWN_POST
    context_url: str = ""       # the post they engaged with
    context_author: str = ""    # whose post it was ('' for our own)
    occurred_at: Optional[datetime] = None
    connection_degree: Optional[str] = None  # '1st'/'2nd'/'3rd+' badge, None when unrendered


@dataclass
class ScoredCandidate:
    """A ranked connection target plus the provenance stored on the request row."""
    person_key: str
    person_name: Optional[str] = None
    person_profile_url: Optional[str] = None
    source: str = SOURCE_OWN_POST
    score: int = 0
    icp_score: int = ICP_UNKNOWN
    warmth_score: int = 0
    signal_count: int = 0
    known_fit: bool = False     # True when we had profile facts to score fit against
    context_url: str = ""
    context_author: str = ""
    reasons: str = ""
    connection_degree: Optional[str] = None
    last_seen_at: Optional[datetime] = None
    signals: list = field(default_factory=list)


def target_terms_from_prefs(prefs: dict) -> list:
    """What "good fit" means for this user, straight from their engagement preferences."""
    terms: list = []
    for key in ("focus_topics", "include_topics", "include_keywords"):
        terms.extend((prefs or {}).get(key) or [])
    return [str(t).strip() for t in terms if str(t or "").strip()]


def warmth_strength(signals: Iterable[CandidateSignal], now: datetime) -> int:
    """0-100 from recency, frequency and source. Repeats add with diminishing returns (1, 1/2, 1/3 …)
    so ten drive-by reactions never outrank someone who keeps showing up on our own posts.
    """
    items = sorted((signals or []),
                   key=lambda s: (s.occurred_at is not None, s.occurred_at), reverse=True)
    total = 0.0
    for i, s in enumerate(items):
        weight = SOURCE_WEIGHTS.get(s.source, SOURCE_WEIGHTS[SOURCE_ADJACENT_POST])
        total += SIGNAL_POINTS * weight * recency_weight(s.occurred_at, now) / (i + 1)
    return max(0, min(100, round(total)))


def _primary_source(signals: Iterable[CandidateSignal]) -> str:
    """Own-post engagement is the headline whenever it happened — it's the stronger signal."""
    return (SOURCE_OWN_POST if any(s.source == SOURCE_OWN_POST for s in signals or [])
            else SOURCE_ADJACENT_POST)


def _describe(candidate_signals: list, icp: int, known_fit: bool, now: datetime,
              degree: str = None) -> str:
    """Plain-language WHY, for the operator approving the request."""
    own = sum(1 for s in candidate_signals if s.source == SOURCE_OWN_POST)
    adjacent = [s for s in candidate_signals if s.source == SOURCE_ADJACENT_POST]
    parts: list = []
    if own:
        parts.append(f"engaged with your content ({own}×)" if own > 1 else "engaged with your content")
    if adjacent:
        authors = sorted({s.context_author for s in adjacent if s.context_author})
        who = f" ({', '.join(authors[:2])})" if authors else ""
        parts.append(f"engaged with adjacent authors' posts{who}")

    stamps = [s.occurred_at for s in candidate_signals if s.occurred_at]
    if stamps:
        days = max(0, int((now - _align(max(stamps), now)).total_seconds() // 86400))
        parts.append("engaged today" if days == 0 else f"last engaged {days}d ago")

    if degree:
        parts.append(f"{degree}-degree")

    if not known_fit:
        parts.append("ICP fit unknown (profile not scraped)")
    elif icp >= 70:
        parts.append("strong ICP fit")
    elif icp < 40:
        parts.append("weak ICP fit")
    return " · ".join(parts)[:512]


def _align(occurred_at: datetime, now: datetime) -> datetime:
    """Match tz-awareness so subtraction can't raise — MySQL hands back naive-UTC datetimes."""
    if (occurred_at.tzinfo is None) != (now.tzinfo is None):
        return occurred_at.replace(tzinfo=now.tzinfo)
    return occurred_at


def group_signals(signals: Iterable[CandidateSignal]) -> dict:
    """Collapse raw signals into {person_key: [signals]}, so the same human found on our post and on
    an adjacent author's post becomes ONE candidate. Keyed by lead_scoring.person_key, so a
    name-only sighting stays separate from a URL one (display names collide — merging them would
    silently blend two people).
    """
    people: dict = {}
    for s in signals or []:
        key = person_key(s.person_name, s.person_profile_url)
        if not key:
            continue
        people.setdefault(key, []).append(s)
    return people


def score_candidate(signals: list, now: datetime, facts: dict = None,
                    target_terms: Iterable[str] = None) -> ScoredCandidate:
    """Score ONE person from every signal we have about them."""
    facts = facts or {}
    known_fit = any(str(facts.get(k) or "").strip() for k in ("job_title", "company_name", "industry"))
    icp = icp_fit(facts, target_terms or [])
    warmth = warmth_strength(signals, now)
    primary = _primary_source(signals)
    # The headline context is the most recent engagement on the strongest source — that's the post
    # the connect note should reference.
    ranked = sorted([s for s in signals if s.source == primary] or list(signals),
                    key=lambda s: (s.occurred_at is not None, s.occurred_at), reverse=True)
    lead_signal = ranked[0]
    stamps = [s.occurred_at for s in signals if s.occurred_at]
    name = next((s.person_name for s in signals if s.person_name), None)
    url = next((s.person_profile_url for s in signals if s.person_profile_url), None)
    # Any sighting that DID render a badge settles the degree; the rest simply didn't show one.
    degree = next((s.connection_degree for s in signals if s.connection_degree), None)
    return ScoredCandidate(
        person_key=person_key(name, url),
        person_name=name,
        person_profile_url=url,
        source=primary,
        score=max(0, min(100, round(ICP_WEIGHT * icp + WARMTH_WEIGHT * warmth))),
        icp_score=icp,
        warmth_score=warmth,
        signal_count=len(signals),
        known_fit=known_fit,
        context_url=lead_signal.context_url or "",
        context_author=lead_signal.context_author or "",
        reasons=_describe(list(signals), icp, known_fit, now, degree=degree),
        connection_degree=degree,
        last_seen_at=max(stamps) if stamps else None,
        signals=list(signals),
    )


def rank_candidates(signals: Iterable[CandidateSignal], now: datetime, facts_by_url: dict = None,
                    target_terms: Iterable[str] = None, min_icp: int = MIN_ICP_DEFAULT,
                    exclude_keys: Iterable[str] = None, require_profile_url: bool = True,
                    exclude_degrees: Iterable[str] = EXCLUDED_DEGREES, limit: int = None) -> list:
    """Rank connection candidates, best first.

    `exclude_keys` are person_keys we already asked (any connection_requests row, any status) — the
    dedup that keeps a nightly re-scan from re-inviting the same person.

    `exclude_degrees` drops people whose scraped badge SAYS we're already connected (issue #623:
    the only connection request production ever filed went to a 1st-degree connection and could
    only fail). A candidate with NO badge is unknown, not connected — it is kept, because LinkedIn
    doesn't render the badge on every surface and failing closed here would stop outreach entirely.

    The `min_icp` floor applies ONLY to people we have profile facts for. Most engagers have never
    been scraped, so they score ICP_UNKNOWN; rejecting them would reject nearly everyone and throw
    away the warm-engager signal this whole feature exists to use. They're kept, ranked on warmth,
    and their `reasons` says the fit is unverified so the approver can see it — and the request row
    stores NO icp_score for them, so nothing is ever filed that reads as below the user's floor.

    `require_profile_url` drops name-only sightings: without a /in/ URL there is nobody to invite.
    """
    facts_by_slug = {profile_slug(k): v for k, v in (facts_by_url or {}).items() if profile_slug(k)}
    excluded = {str(k) for k in (exclude_keys or []) if k}
    blocked_degrees = {str(d).lower() for d in (exclude_degrees or []) if d}
    out: list = []
    for key, person_signals in group_signals(signals).items():
        if key in excluded:
            continue
        candidate = score_candidate(person_signals, now,
                                    facts=facts_by_slug.get(profile_slug(
                                        next((s.person_profile_url for s in person_signals
                                              if s.person_profile_url), "")), {}),
                                    target_terms=target_terms)
        if require_profile_url and not candidate.person_profile_url:
            continue
        if str(candidate.connection_degree or "").lower() in blocked_degrees:
            continue
        if candidate.known_fit and candidate.icp_score < int(min_icp or 0):
            continue
        out.append(candidate)
    out.sort(key=lambda c: (c.score, c.warmth_score, c.signal_count), reverse=True)
    return out if limit is None else out[:max(0, int(limit))]


def first_name(name: str = None) -> str:
    """First token of a scraped display name, safe to drop straight into a greeting.

    Never empty and never raises on junk: a missing or unscrapable name becomes ``"there"``, so
    `default_connect_note` writes "Hi there, ..." instead of "Hi , ...". That is the same fallback
    `run_automation.render_dm_placeholders` fills `{first_name}` with, so a connect note and a DM
    read the same way when the name is unknown.
    """
    cleaned = " ".join(str(name or "").split()).strip()
    return cleaned.split(" ")[0] if cleaned else "there"


def default_connect_note(candidate: ScoredCandidate, topic: str = None) -> str:
    """The pre-AI connect note. Grounded in the actual reason we found them — a note that names the
    shared context reads like a human wrote it, which is the whole point of warm targeting.
    Kept under LinkedIn's 300-char note limit even before refinement.
    """
    name = first_name(candidate.person_name)
    about = f" about {topic}" if topic else ""
    if candidate.source == SOURCE_ROSTER:
        # The honest shared ground for a roster target (issue #979): we read and comment on their
        # posts. No pitch, no ask beyond the connection — this note goes to a peer or creator the
        # user chose to follow, not to a prospect.
        note = (f"Hi {name}, I read your posts{about} and comment where I can — I'd love to connect "
                f"so I can keep up with them properly.")
    elif candidate.source == SOURCE_ADJACENT_POST and candidate.context_author:
        note = (f"Hi {name}, I saw your take on {candidate.context_author}'s post{about} — we're "
                f"clearly following the same conversation. Would love to connect and keep "
                f"trading notes.")
    elif candidate.source == SOURCE_ADJACENT_POST:
        note = (f"Hi {name}, we keep showing up in the same conversations{about} — would love to "
                f"connect and keep trading notes.")
    else:
        note = (f"Hi {name}, thanks for engaging with my posts{about} — really appreciate it. "
                f"Would love to connect so we can keep the conversation going.")
    return note[:CONNECT_NOTE_LIMIT]
