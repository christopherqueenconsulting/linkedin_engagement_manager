"""Post-time recommendations and content-attribution ranking from captured engagement stats
(pure functions). Scoring is impression-normalized (engagement RATE) whenever every row in the
comparison set carries impressions, and always recency-weighted so stale posts fade out.
"""

from collections import defaultdict
from datetime import datetime, timezone, tzinfo
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cqc_lem.domain.models import PostEngagementRow

_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# The attribution snapshot columns of a `PostEngagementRow` — which hooks/formats/topics a post was
# written as. Names, not indexes: the row layout lives in ONE place now (issue #1220).
ATTRIBUTE_FIELDS = ("archetype", "hook_style", "format", "topic", "buyer_stage")

# Half-life of a post's influence on the recommendation. ~1 month keeps the last few weeks
# dominant without discarding the older sample entirely.
RECENCY_HALF_LIFE_DAYS = 30.0

# Shrinkage prior on the effective (recency-weighted) sample size. Without it a single lucky —
# or a single ancient — post could top the ranking; with it a bucket has to earn its rank with
# enough RECENT evidence.
SUPPORT_PRIOR = 1.0

METRIC_RATE = "engagement_rate"
METRIC_COUNT = "engagement"


def engagement_score(reactions: Optional[int], comments: Optional[int],
                     reposts: Optional[int] = 0) -> int:
    """Weight deeper signals higher (2026: comments/reposts >> likes)."""
    return int(reactions or 0) + 2 * int(comments or 0) + 2 * int(reposts or 0)


def engagement_rate(reactions: Optional[int], comments: Optional[int], reposts: Optional[int] = 0,
                    impressions: Optional[int] = None) -> Optional[float]:
    """Impression-normalized engagement — `engagement_score` per impression. None when impressions
    are unknown or zero so callers can fall back to raw counts instead of scoring a post as 0.
    """
    views = int(impressions or 0)
    if views <= 0:
        return None
    return engagement_score(reactions, comments, reposts) / views


def recency_weight(scheduled_time: Optional[datetime], now: Optional[datetime] = None,
                   half_life_days: float = RECENCY_HALF_LIFE_DAYS) -> float:
    """Exponential half-life decay on a post's age. Missing/future timestamps and mixed timezone
    awareness degrade to 1.0 (unweighted) rather than distorting the average.
    """
    if scheduled_time is None:
        return 1.0
    if now is None:
        now = datetime.now(tz=getattr(scheduled_time, "tzinfo", None))
    try:
        age_days = (now - scheduled_time).total_seconds() / 86400.0
    except TypeError:
        return 1.0
    if age_days <= 0 or half_life_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


def _named(rows: Iterable[Sequence]) -> list:
    """Every non-empty row as a `PostEngagementRow`, materialized once.

    `rows` may be a one-shot cursor iterable, and a SHORT row pads to None — the four-column
    minimum this module documents.
    """
    return [PostEngagementRow.from_row(row) for row in rows if row]


def _rate_mode(rows: Sequence) -> bool:
    """True only when EVERY row has impressions, so rate and per-post counts are never mixed on
    one ranking (mirrors the same gate in `content_framework.performance_weights`).
    """
    return bool(rows) and all(int(r.impressions or 0) > 0 for r in rows)


def _row_metric(row: PostEngagementRow, rate_mode: bool) -> float:
    if rate_mode:
        rate = engagement_rate(row.reactions, row.comments, row.reposts, row.impressions)
        if rate is not None:
            return rate
    return float(engagement_score(row.reactions, row.comments, row.reposts))


def _round(value: float, rate_mode: bool) -> float:
    return round(value, 5) if rate_mode else round(value, 1)


def _group_metrics(rows: Iterable[PostEngagementRow], rate_mode: bool, now: Optional[datetime],
                   half_life_days: float, prior: float = SUPPORT_PRIOR) -> Tuple[float, dict]:
    """Score one group of rows (a time bucket or an attribute value): the recency-weighted mean
    metric, its `support` (sum of recency weights = effective recent sample size) and the ranking
    `score`, which shrinks the mean toward 0 by support/(support + prior). Shrinkage is what makes
    the recency weighting bite ACROSS groups too — a group whose only evidence is old has little
    support left, so it cannot outrank a well-sampled recent one on a stale fluke.

    Returns (unrounded score, payload) — callers rank on the unrounded value so display rounding
    can never flip the ordering, and only ever emit the rounded `score`.
    """
    weighted = 0.0
    support = 0.0
    samples = 0
    for row in rows:
        weight = recency_weight(row.scheduled_time, now=now, half_life_days=half_life_days)
        weighted += weight * _row_metric(row, rate_mode)
        support += weight
        samples += 1
    average = weighted / support if support > 0 else 0.0
    score = average * (support / (support + prior)) if support + prior > 0 else 0.0
    return score, {"avg_engagement": _round(average, rate_mode), "score": _round(score, rate_mode),
                   "support": round(support, 3), "samples": samples,
                   "metric": METRIC_RATE if rate_mode else METRIC_COUNT}


def _local_wall_clock(scheduled: datetime, zone: Optional[tzinfo]) -> datetime:
    """Stored scheduled_time is naive UTC (docs/timezone-contract.md); a "best hour" is only
    meaningful as the AUDIENCE's wall clock, so shift it into the user's zone before bucketing.
    """
    if zone is None:
        return scheduled
    aware = scheduled.replace(tzinfo=timezone.utc) if scheduled.tzinfo is None else scheduled
    return aware.astimezone(zone)


def recommend_post_times(rows: Iterable[Sequence], top_n: int = 3, min_posts: int = 3,
                         now: Optional[datetime] = None,
                         half_life_days: float = RECENCY_HALF_LIFE_DAYS,
                         tz: Optional[str] = None) -> list:
    """rows: iterable of `db.get_post_engagement_rows` tuples (at minimum
    (scheduled_time[datetime], reactions, comments, reposts)). Returns the top (weekday, hour)
    buckets by recency-weighted average engagement per post — or engagement RATE when every row
    has impressions. Empty until >= min_posts of data so we don't recommend off noise.

    `tz` is the user's IANA timezone: the returned weekday/hour are that zone's wall clock, which is
    what callers need — get_post_time hands the hour straight back to the scheduler as a LOCAL time
    to be converted to UTC for storage, so bucketing the raw UTC hour would shift every
    recommendation by the user's offset twice. Omitted (or unknown) means UTC.
    """
    usable = [row for row in _named(rows) if row.scheduled_time is not None]
    if len(usable) < min_posts:
        return []
    zone = None
    if tz:
        try:
            zone = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            zone = None
    rate_mode = _rate_mode(usable)
    buckets = defaultdict(list)
    for row in usable:
        scheduled = _local_wall_clock(row.scheduled_time, zone)
        buckets[(scheduled.weekday(), scheduled.hour)].append(row)
    ranked = []
    for (weekday, hour), bucket_rows in buckets.items():
        raw_score, metrics = _group_metrics(bucket_rows, rate_mode, now, half_life_days)
        ranked.append((raw_score, {"weekday": _WEEKDAYS[weekday], "weekday_num": weekday,
                                   "hour": hour, "sample": metrics.pop("samples"), **metrics}))
    ranked.sort(key=lambda entry: (-entry[0], -entry[1]["sample"], entry[1]["weekday_num"],
                                   entry[1]["hour"]))
    return [bucket for _, bucket in ranked[:top_n]]


def rank_content_attributes(rows: Iterable[Sequence], attributes: Optional[Iterable[str]] = None,
                            top_n: Optional[int] = None, min_samples: int = 1,
                            now: Optional[datetime] = None,
                            half_life_days: float = RECENCY_HALF_LIFE_DAYS) -> dict:
    """Which hooks/formats/topics actually win — ranks the attribution snapshot columns carried on
    each stat row (#386/B1) by the same recency-weighted, impression-normalized metric as
    `recommend_post_times`. Returns
    {attribute: [{"key", "score", "avg_engagement", "support", "samples", "metric"}, ...]} sorted
    best-first; attribute values seen fewer than `min_samples` times are dropped, and attributes
    with no qualifying data map to an empty list.
    """
    names = list(attributes) if attributes else list(ATTRIBUTE_FIELDS)
    materialized = _named(rows)
    result = {}
    for name in names:
        if name not in ATTRIBUTE_FIELDS:
            result[name] = []
            continue
        groups = defaultdict(list)
        for row in materialized:
            key = getattr(row, name)
            if key is not None and key != "":
                groups[key].append(row)
        groups = {key: group for key, group in groups.items() if len(group) >= min_samples}
        # Scale is decided across the whole attribute so its keys stay comparable to each other.
        rate_mode = _rate_mode([row for group in groups.values() for row in group])
        scored = []
        for key, group in groups.items():
            raw_score, metrics = _group_metrics(group, rate_mode, now, half_life_days)
            scored.append((raw_score, {"key": key, **metrics}))
        scored.sort(key=lambda entry: (-entry[0], -entry[1]["samples"], str(entry[1]["key"])))
        ranked = [entry for _, entry in scored]
        result[name] = ranked[:top_n] if top_n else ranked
    return result


def select_variant_winners(rows: Iterable[Mapping], top_n: Optional[int] = None,
                           min_samples: int = 1, now: Optional[datetime] = None,
                           half_life_days: float = RECENCY_HALF_LIFE_DAYS) -> dict:
    """Outcome-tracked A/B winner selection (issue #396 / D2). `rows` are the dicts from
    `db.get_variant_outcome_rows` — each a post that SHIPPED a given `variant_key`, carrying its
    latest captured signals. Groups by variant_key and ranks by the SAME recency-weighted,
    impression-normalized metric as `rank_content_attributes`, so a variant only wins on enough
    RECENT evidence (a single stale fluke can't top the ranking). Returns
    ``{"winner": <key or None>, "ranking": [{"key","score","avg_engagement","support","samples",
    "metric"}, ...]}`` best-first; keys seen fewer than `min_samples` times are dropped. Pure — no
    DB. The ranking entries share `rank_content_attributes`' shape so B3/B4 consumers can read
    them the same way.
    """
    groups: dict = defaultdict(list)
    for r in rows:
        if not r:
            continue
        key = r.get("variant_key")
        if key is None or key == "":
            continue
        # Named-column row so the shared scoring machinery applies; the attribution columns stay
        # None because a variant outcome carries its own key instead.
        groups[key].append(PostEngagementRow(
            scheduled_time=r.get("scheduled_time"), reactions=r.get("reactions"),
            comments=r.get("comments"), reposts=r.get("reposts"),
            impressions=r.get("impressions")))
    groups = {key: group for key, group in groups.items() if len(group) >= min_samples}
    # Scale is decided across every variant so their keys stay comparable to each other.
    rate_mode = _rate_mode([row for group in groups.values() for row in group])
    scored = []
    for key, group in groups.items():
        raw_score, metrics = _group_metrics(group, rate_mode, now, half_life_days)
        scored.append((raw_score, {"key": key, **metrics}))
    scored.sort(key=lambda entry: (-entry[0], -entry[1]["samples"], str(entry[1]["key"])))
    ranking = [entry for _, entry in scored]
    if top_n:
        ranking = ranking[:top_n]
    return {"winner": ranking[0]["key"] if ranking else None, "ranking": ranking}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _impressions(value: Any) -> Optional[int]:
    """Impressions normalized to a positive count or None — mirrors `engagement_rate`'s treatment
    of 0/missing impressions as unknown, so a row never shows `impressions: 0` beside a null rate.
    """
    views = _int(value)
    return views if views > 0 else None


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else (value or None)


def build_performance_table(rows: Iterable[Mapping]) -> list:
    """Per-post performance rows for the analytics dashboard (#395). Takes the dicts from
    `db.get_post_performance_rows` and returns JSON-ready rows newest-first, each with its raw
    signals plus the derived `engagement` (weighted count) and `engagement_rate` (per impression;
    None when impressions are unknown). Pure — no DB and no recency weighting: the table shows the
    actual per-post outcome, not the recency-decayed ranking metric used by `recommend_post_times`
    / `rank_content_attributes`.
    """
    out = []
    for r in rows:
        if not r:
            continue
        reactions, comments, reposts = r.get("reactions"), r.get("comments"), r.get("reposts")
        rate = engagement_rate(reactions, comments, reposts, r.get("impressions"))
        out.append({
            "post_id": r.get("post_id"),
            "scheduled_time": _iso(r.get("scheduled_time")),
            "format": r.get("format"),
            "archetype": r.get("archetype"),
            "hook_style": r.get("hook_style"),
            "topic": r.get("topic"),
            "buyer_stage": r.get("buyer_stage"),
            "reactions": _int(reactions),
            "comments": _int(comments),
            "reposts": _int(reposts),
            "saves": _int(r.get("saves")),
            "impressions": _impressions(r.get("impressions")),
            "engagement": engagement_score(reactions, comments, reposts),
            "engagement_rate": round(rate, 5) if rate is not None else None,
        })
    out.sort(key=lambda row: row["scheduled_time"] or "", reverse=True)
    return out


def build_engagement_trend(rows: Iterable[Mapping]) -> list:
    """Daily engagement-rate / impression trend for the analytics dashboard (#395). Buckets the
    per-post rows by calendar day (post date), sums each signal, and returns points ascending in
    time. `engagement_rate` is filled only when EVERY post that day carried impressions — otherwise
    the day's impression total is incomplete and a rate would mislead, so both `impressions` and
    `engagement_rate` come back None (the same rate-mode gate `post_stats` uses elsewhere). Pure —
    no DB.
    """
    buckets: dict = {}
    for r in rows:
        if not r:
            continue
        scheduled = r.get("scheduled_time")
        day = scheduled.date() if hasattr(scheduled, "date") else None
        if day is None:
            continue
        bucket = buckets.setdefault(day, {"reactions": 0, "comments": 0, "reposts": 0, "saves": 0,
                                          "impressions": 0, "impressions_complete": True, "posts": 0})
        bucket["reactions"] += _int(r.get("reactions"))
        bucket["comments"] += _int(r.get("comments"))
        bucket["reposts"] += _int(r.get("reposts"))
        bucket["saves"] += _int(r.get("saves"))
        views = _impressions(r.get("impressions"))
        if views is None:  # missing OR non-positive → day's impression total is unknown
            bucket["impressions_complete"] = False
        else:
            bucket["impressions"] += views
        bucket["posts"] += 1
    trend = []
    for day in sorted(buckets):
        b = buckets[day]
        complete = b["impressions_complete"] and b["impressions"] > 0
        rate = (engagement_rate(b["reactions"], b["comments"], b["reposts"], b["impressions"])
                if complete else None)
        trend.append({
            "date": day.isoformat(),
            "reactions": b["reactions"], "comments": b["comments"], "reposts": b["reposts"],
            "saves": b["saves"],
            "impressions": b["impressions"] if b["impressions_complete"] else None,
            "engagement": engagement_score(b["reactions"], b["comments"], b["reposts"]),
            "engagement_rate": round(rate, 5) if rate is not None else None,
            "posts": b["posts"],
        })
    return trend
