"""Versioned, explainable ICP rubric for connection targets (issue #2630, Phase 1).

Pure — no DB, no Selenium, no LLM. Hard filters run FIRST and decide eligibility; only an eligible
person is scored, by fixed weights that sum to 100. Every result carries per-component points and a
one-line reason, so whoever approves the request can see why the person was chosen.

Missing facts are never a hard filter: an unknown industry, company size, geography or timing signal
earns neutral partial credit. A person with NO scraped facts at all is the caller's to hold for a
profile scrape — this module never scores one blind (`has_facts`).
"""

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from pydantic import BaseModel, Field, ValidationError

# Bump on ANY change to a weight, a filter or a component formula: stored on every filed target so
# outcomes can later be compared across rubric versions.
ICP_RUBRIC_VERSION = "v1"

WEIGHT_ROLE = 30
WEIGHT_FIRMOGRAPHIC = 25
WEIGHT_ENGAGEMENT = 25
WEIGHT_REACHABILITY = 10
WEIGHT_TIMING = 10
WEIGHTS: dict = {"role": WEIGHT_ROLE, "firmographic": WEIGHT_FIRMOGRAPHIC,
                 "engagement": WEIGHT_ENGAGEMENT, "reachability": WEIGHT_REACHABILITY,
                 "timing": WEIGHT_TIMING}

# What an unknown fact earns: half the points on offer, so a gap neither rewards nor sinks a person.
NEUTRAL = 0.5

_FACT_KEYS = ("job_title", "company_name", "industry")
_REACH_BY_DEGREE = {"2nd": 1.0, "3rd+": 0.6, "3rd": 0.6}

# Why a person was filtered out — stable strings, asserted by tests and read by the funnel log.
FILTER_DO_NOT_CONTACT = "do_not_contact"
FILTER_CLIENT = "existing_client"
FILTER_COMPETITOR = "competitor"
FILTER_CONNECTED = "already_connected"
FILTER_RECENT_TOUCH = "recently_touched"
FILTER_SENIORITY = "excluded_seniority"
FILTER_NO_SEGMENT = "no_segment_match"


class IcpSegment(BaseModel):
    """One buyer segment. An empty list means "no constraint on this axis"."""

    id: str = Field(min_length=1, max_length=64)
    name: str = ""
    role_titles: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=list)
    function: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    company_size_min: Optional[int] = Field(default=None, ge=0)
    company_size_max: Optional[int] = Field(default=None, ge=0)
    geographies: list[str] = Field(default_factory=list)
    pains: list[str] = Field(default_factory=list)
    trigger_phrases: list[str] = Field(default_factory=list)


class IcpExclusions(BaseModel):
    """People who must never be filed, whatever they score."""

    clients: list[str] = Field(default_factory=list)          # company names
    competitors: list[str] = Field(default_factory=list)      # company names
    do_not_contact: list[str] = Field(default_factory=list)   # profile slugs
    seniority: list[str] = Field(default_factory=list)        # title words, e.g. "intern"


class IcpDefinition(BaseModel):
    """A user's structured ideal customer profile."""

    segments: list[IcpSegment] = Field(default_factory=list)
    exclusions: IcpExclusions = Field(default_factory=IcpExclusions)


def parse_icp_definition(raw) -> Optional[IcpDefinition]:
    """A stored definition as `IcpDefinition`, or None when absent, malformed or segment-less.

    None means "use today's `icp_fit`" — a bad definition must never silently filter everyone out.
    """
    if not raw or not isinstance(raw, dict):
        return None
    try:
        definition = IcpDefinition.model_validate(raw)
    except ValidationError:
        return None
    return definition if definition.segments else None


@dataclass
class IcpScore:
    """The rubric's answer for one person."""

    eligible: bool
    total: int = 0
    components: dict = field(default_factory=dict)
    segment_id: Optional[str] = None
    reason: str = ""
    filtered_by: Optional[str] = None
    rubric_version: str = ICP_RUBRIC_VERSION


def has_facts(facts: Optional[dict]) -> bool:
    """True when we scraped at least a title, company or industry for this person."""
    return any(str((facts or {}).get(k) or "").strip() for k in _FACT_KEYS)


def _norm(value) -> str:
    return " ".join(str(value or "").lower().split())


def _mentions(text: str, term: str) -> bool:
    """Whole-word match — 'cto' is inside 'director', so substrings mis-score."""
    term = _norm(term)
    return bool(term) and bool(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text))


def _any(text: str, terms: Iterable[str]) -> bool:
    return any(_mentions(text, t) for t in terms or [])


def _slug(value) -> str:
    match = re.search(r"/in/([^/?#]+)", str(value or ""))
    return (match.group(1) if match else str(value or "")).strip().lower()


def _to_int(value) -> Optional[int]:
    try:
        return int(value) if value is not None and str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _hard_filter(definition: IcpDefinition, facts: dict, profile_url: str,
                 connection_degree: Optional[str], recently_touched: bool) -> Optional[str]:
    """The first exclusion that applies, or None."""
    ex = definition.exclusions
    title = _norm(facts.get("job_title"))
    company = _norm(facts.get("company_name"))
    slug = _slug(profile_url)
    if slug and slug in {_slug(s) for s in ex.do_not_contact if s}:
        return FILTER_DO_NOT_CONTACT
    if company and _any(company, ex.clients):
        return FILTER_CLIENT
    if company and _any(company, ex.competitors):
        return FILTER_COMPETITOR
    if _norm(connection_degree).startswith("1st"):
        return FILTER_CONNECTED
    if recently_touched:
        return FILTER_RECENT_TOUCH
    if title and _any(title, ex.seniority):
        return FILTER_SENIORITY
    return None


def _segment_eligible(segment: IcpSegment, facts: dict) -> bool:
    """Role, industry, size and geography filters. A KNOWN mismatch fails; an unknown never does."""
    title = _norm(facts.get("job_title"))
    industry = _norm(facts.get("industry"))
    if segment.role_titles and title and not _any(title, segment.role_titles):
        return False
    if segment.industries and industry and not _any(industry, segment.industries):
        return False
    size = _to_int(facts.get("company_size"))
    if size is not None:
        if segment.company_size_min is not None and size < segment.company_size_min:
            return False
        if segment.company_size_max is not None and size > segment.company_size_max:
            return False
    location = _norm(facts.get("location"))
    if segment.geographies and location and not _any(location, segment.geographies):
        return False
    return True


def _role_points(segment: IcpSegment, facts: dict) -> float:
    title = _norm(facts.get("job_title"))
    # An unread title is unknown, not a mismatch: neutral credit, same as size and geography.
    title_fit = (1.0 if _any(title, segment.role_titles) else 0.0) if segment.role_titles and title else NEUTRAL
    level_terms = list(segment.seniority) + list(segment.function)
    level_fit = (1.0 if _any(title, level_terms) else 0.0) if level_terms and title else NEUTRAL
    return WEIGHT_ROLE * (0.6 * title_fit + 0.4 * level_fit)


def _firmographic_points(segment: IcpSegment, facts: dict) -> float:
    industry = _norm(facts.get("industry"))
    if segment.industries and industry:
        industry_fit = 1.0
    else:
        industry_fit = NEUTRAL
    has_band = segment.company_size_min is not None or segment.company_size_max is not None
    size_fit = 1.0 if has_band and _to_int(facts.get("company_size")) is not None else NEUTRAL
    geo_fit = 1.0 if segment.geographies and _norm(facts.get("location")) else NEUTRAL
    return WEIGHT_FIRMOGRAPHIC * (0.4 * industry_fit + 0.32 * size_fit + 0.28 * geo_fit)


def _timing_points(facts: dict) -> float:
    changed = facts.get("recent_job_change")
    if changed is None:
        return WEIGHT_TIMING * NEUTRAL
    return float(WEIGHT_TIMING if changed else 0)


def score_person(definition: IcpDefinition, facts: dict, profile_url: str = "",
                 warmth: int = 0, connection_degree: Optional[str] = None,
                 recently_touched: bool = False) -> IcpScore:
    """Filters first, then the weighted rubric against the best-fitting segment.

    Args:
        definition: The user's ICP.
        facts: Scraped profile facts (`job_title`, `company_name`, `industry`; optional
            `company_size`, `location`, `recent_job_change`). Callers hold a fact-less person.
        profile_url: The person's profile URL, matched against do-not-contact.
        warmth: 0-100 engagement strength (recency-weighted), from `warmth_strength`.
        connection_degree: Scraped badge; unknown is treated as reachable.
        recently_touched: Whether the caller found a recent touch inside the cooldown.

    Returns:
        An `IcpScore`; `eligible=False` carries `filtered_by` and a total of 0.
    """
    facts = facts or {}
    blocked = _hard_filter(definition, facts, profile_url, connection_degree, recently_touched)
    if blocked:
        return IcpScore(False, 0, {}, None, f"filtered: {blocked}", blocked)

    fitting = [s for s in definition.segments if _segment_eligible(s, facts)]
    if not fitting:
        return IcpScore(False, 0, {}, None, f"filtered: {FILTER_NO_SEGMENT}", FILTER_NO_SEGMENT)

    engagement = WEIGHT_ENGAGEMENT * max(0, min(100, int(warmth))) / 100
    reach = WEIGHT_REACHABILITY * _REACH_BY_DEGREE.get(_norm(connection_degree), NEUTRAL)
    timing = _timing_points(facts)

    best: Optional[IcpScore] = None
    for segment in fitting:
        components = {"role": _role_points(segment, facts),
                      "firmographic": _firmographic_points(segment, facts),
                      "engagement": engagement, "reachability": reach, "timing": timing}
        total = max(0, min(100, round(sum(components.values()))))
        if best is None or total > best.total:
            best = IcpScore(True, total, {k: round(v, 1) for k, v in components.items()},
                            segment.id, "", None)
    segment = next(s for s in fitting if s.id == best.segment_id)
    best.reason = _reason(segment, best, facts)
    return best


def _reason(segment: IcpSegment, score: IcpScore, facts: dict) -> str:
    """One line: who, which segment, and the strongest and weakest component."""
    who = " at ".join(p for p in (str(facts.get("job_title") or "").strip(),
                                  str(facts.get("company_name") or "").strip()) if p) or "profile"
    ranked = sorted(score.components.items(), key=lambda kv: kv[1] / WEIGHTS[kv[0]], reverse=True)
    strong, weak = ranked[0][0], ranked[-1][0]
    label = segment.name or segment.id
    return (f"{who} fits segment '{label}' ({score.total}/100, rubric {score.rubric_version}); "
            f"strongest: {strong}, weakest: {weak}")[:512]
