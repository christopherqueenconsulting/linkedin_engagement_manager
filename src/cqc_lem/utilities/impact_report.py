"""The impact report: how one change performed, 14 days before against 14 days after (issue #2643).

PRIVATE by design (the repo is public). The report is read through the admin-only
`GET /api/admin/impact-report` and emailed to the owner; the only thing that ever reaches GitHub is
`READY_NOTE`, which carries no metric, name or URL.

`build_impact_report` is pure: already-read inputs in, one payload out. `load_impact_report` does
the reads and raises `ImpactReadError` on ANY failed read — a window that could not be read is an
error, never an empty report. Method (the issue's section 3): the deploy's ET day and any outage
day are excluded from both windows, every metric is compared on the same definition, and a
comparison with too few events is `inconclusive`, never a direction.

Days are America/New_York days. `logs` / `leads` `created_at` are read as naive UTC (the app's
storage convention) and folded into ET days from hourly buckets.
"""

import re
from datetime import date, datetime, time, timedelta, timezone
from statistics import median
from typing import Any, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from cqc_lem.platform.db.enums import (
    COMMENT_LOG_ACTION_TYPES,
    INVITE_LOG_ACTION_TYPES,
    LogActionType,
    LogResultType,
)
from cqc_lem.utilities.post_stats import third_party_engagement_rate

ET = ZoneInfo("America/New_York")
DEFAULT_DAYS = 14
MAX_DAYS = 60
# Below these, a before/after difference is noise and the verdict is "inconclusive".
MIN_POSTS = 5
MIN_EVENTS = 10
MIN_SNAPSHOTS = 2
MIN_COST_DAYS = 5
# A relative move smaller than this is "flat".
FLAT_BAND = 0.05

# The ONLY text this feature ever posts to GitHub: no metric, no name, no URL.
READY_NOTE = "Impact report ready for {tag}."
_TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+$")

ALWAYS_NOT_MEASURED = {
    "author_replies": "author replies (reply reader is not trustworthy yet, #2621)",
    "saves": "saves (not collected)",
    "catch_up_sends": "catch-up sends (no distinct log marker)",
    "authenticity_score": "post authenticity score (not stored per post)",
    "meetings_won": "meetings and won (not tracked)",
}


class ImpactReadError(RuntimeError):
    """A read the report depends on failed; the caller answers 503, never an empty report."""


def release_note(tag: str) -> Optional[str]:
    """The one-line GitHub note for `tag`, or None when the tag is not a plain version.

    A tag that is not `vX.Y.Z` could carry anything, so it posts nothing rather than echo it.
    """
    return READY_NOTE.format(tag=tag) if isinstance(tag, str) and _TAG_RE.match(tag) else None


def et_day(moment: datetime) -> date:
    """The America/New_York calendar day of a moment; a naive one is read as UTC."""
    aware = moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment
    return aware.astimezone(ET).date()


def _hour_day(hour: str) -> date:
    """ET day of a `YYYY-MM-DD HH` UTC hour bucket."""
    return et_day(datetime.strptime(hour, "%Y-%m-%d %H"))


def window_days(change_at: datetime, days: int,
                exclude_days: Iterable[date] = ()) -> tuple[list[date], list[date]]:
    """(before, after) ET days: `days` before the change's day and `days` after it.

    The change's own day belongs to neither window, and every day in `exclude_days` (an outage) is
    dropped from both.
    """
    skip = set(exclude_days)
    deploy = et_day(change_at)
    before = [deploy - timedelta(days=i) for i in range(days, 0, -1)]
    after = [deploy + timedelta(days=i) for i in range(1, days + 1)]
    return [d for d in before if d not in skip], [d for d in after if d not in skip]


def utc_bounds(change_at: datetime, days: int) -> tuple[datetime, datetime]:
    """The UTC span [start, end) covering both windows, from ET midnights."""
    deploy = et_day(change_at)
    start = datetime.combine(deploy - timedelta(days=days), time.min, tzinfo=ET)
    end = datetime.combine(deploy + timedelta(days=days + 1), time.min, tzinfo=ET)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def verdict(before: Optional[float], after: Optional[float], n_before: int, n_after: int,
            min_n: int) -> str:
    """`up` / `down` / `flat`, or `inconclusive` when a side is unread or n is below `min_n`."""
    if before is None or after is None or n_before < min_n or n_after < min_n:
        return "inconclusive"
    if before == 0:
        return "flat" if after == 0 else "up"
    change = (after - before) / abs(before)
    if abs(change) < FLAT_BAND:
        return "flat"
    return "up" if change > 0 else "down"


def _metric(before: Optional[float], after: Optional[float], n_before: int, n_after: int,
            min_n: int) -> dict[str, Any]:
    delta = None if before is None or after is None else round(after - before, 4)
    return {"before": before, "after": after, "delta": delta, "n_before": n_before,
            "n_after": n_after, "verdict": verdict(before, after, n_before, n_after, min_n)}


def _rate(total: int, day_count: int) -> Optional[float]:
    return round(total / day_count, 4) if day_count else None


def _pooled_organic_rate(rows: Sequence[Mapping[str, Any]]) -> tuple[Optional[float], int]:
    """Impression-weighted organic engagement rate over posts with a full reading, and that n."""
    score = impressions = n = 0
    for r in rows:
        rate = third_party_engagement_rate(r.get("reactions"), r.get("comments"),
                                           r.get("own_comments"), r.get("reposts"),
                                           r.get("impressions"))
        if rate is not None and r.get("impressions"):
            score += rate * int(r["impressions"])
            impressions += int(r["impressions"])
            n += 1
    return (round(score / impressions, 6) if impressions else None), n


def _sum_by_day(rows: Iterable[Mapping[str, Any]], days: set[date], keep) -> int:
    return sum(int(r["n"]) for r in rows if keep(r) and _hour_day(r["hour"]) in days)


def _follower_net(snapshots: Sequence[Mapping[str, Any]], days: set[date]) -> tuple[Optional[int], int]:
    """Last minus first follower count captured inside the window's days, and the snapshot count."""
    inside = sorted((s for s in snapshots
                     if s.get("captured_at") is not None and s.get("follower_count") is not None
                     and et_day(s["captured_at"]) in days), key=lambda s: s["captured_at"])
    if len(inside) < MIN_SNAPSHOTS:
        return None, len(inside)
    return int(inside[-1]["follower_count"]) - int(inside[0]["follower_count"]), len(inside)


def build_impact_report(change_at: datetime, days: int, exclude_days: Iterable[date],
                        posts: Sequence[Mapping[str, Any]], counts: Mapping[str, Sequence],
                        followers: Sequence[Mapping[str, Any]],
                        cost: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One payload comparing the windows; pure.

    Args:
        change_at: When the change shipped (aware UTC).
        days: Window length in days on each side.
        exclude_days: ET days to drop from both windows (outages).
        posts: `get_impact_post_rows` output (latest `post_stats` per post).
        counts: `get_impact_hourly_counts` output.
        followers: `get_follower_stats` output.
        cost: `get_impact_llm_cost_by_day` output.

    Returns:
        `windows`, `metrics` (each with before/after/delta/n/verdict), and `not_measured`.
    """
    before_days, after_days = window_days(change_at, days, exclude_days)
    sets = {"before": set(before_days), "after": set(after_days)}
    logs, leads = counts.get("logs") or [], counts.get("leads") or []

    def posts_in(side: str) -> list:
        return [p for p in posts if p.get("scheduled_time") is not None
                and et_day(p["scheduled_time"]) in sets[side]]

    def per_day(side: str, rows, keep) -> tuple[Optional[float], int]:
        total = _sum_by_day(rows, sets[side], keep)
        return _rate(total, len(sets[side])), total

    def count_metric(rows, keep) -> dict:
        b, nb = per_day("before", rows, keep)
        a, na = per_day("after", rows, keep)
        return _metric(b, a, nb, na, MIN_EVENTS)

    def log_is(types: tuple, result: LogResultType):
        return lambda r: r.get("action_type") in types and r.get("result") == result.value

    pb, pa = posts_in("before"), posts_in("after")
    imp_b = [int(p["impressions"]) for p in pb if p.get("impressions")]
    imp_a = [int(p["impressions"]) for p in pa if p.get("impressions")]
    rate_b, rn_b = _pooled_organic_rate(pb)
    rate_a, rn_a = _pooled_organic_rate(pa)
    fol_b, fn_b = _follower_net(followers, sets["before"])
    fol_a, fn_a = _follower_net(followers, sets["after"])

    def cost_side(side: str) -> tuple[Optional[float], int]:
        rows = [float(c["usd"]) for c in cost if c["day"] in sets[side]]
        return (round(sum(rows) / len(sets[side]), 4) if rows and sets[side] else None), len(rows)

    cost_b, cn_b = cost_side("before")
    cost_a, cn_a = cost_side("after")
    dm_ok = (LogActionType.DM.value,)

    metrics = {
        "posts_published_per_day": _metric(_rate(len(pb), len(sets["before"])),
                                           _rate(len(pa), len(sets["after"])),
                                           len(pb), len(pa), MIN_POSTS),
        "median_impressions": _metric(median(imp_b) if imp_b else None,
                                      median(imp_a) if imp_a else None,
                                      len(imp_b), len(imp_a), MIN_POSTS),
        "organic_engagement_rate": _metric(rate_b, rate_a, rn_b, rn_a, MIN_POSTS),
        "net_followers": _metric(fol_b, fol_a, fn_b, fn_a, MIN_SNAPSHOTS),
        "comments_per_day": count_metric(logs, log_is(COMMENT_LOG_ACTION_TYPES, LogResultType.SUCCESS)),
        "dm_sends_per_day": count_metric(logs, log_is(dm_ok, LogResultType.SUCCESS)),
        "dm_failures_per_day": count_metric(logs, log_is(dm_ok, LogResultType.FAILURE)),
        "invites_per_day": count_metric(logs, log_is(INVITE_LOG_ACTION_TYPES, LogResultType.SUCCESS)),
        "new_leads_per_day": count_metric(leads, lambda r: True),
        "new_in_conversation_per_day": count_metric(
            leads, lambda r: r.get("stage") in ("in_conversation", "opportunity")),
        "llm_cost_usd_per_day": _metric(cost_b, cost_a, cn_b, cn_a, MIN_COST_DAYS),
    }
    unread = {
        "median_impressions": "median impressions (no post had impressions captured)",
        "organic_engagement_rate": "organic engagement rate (own-comment count or impressions missing)",
        "net_followers": "net followers (fewer than two snapshots in a window)",
        "llm_cost_usd_per_day": "LLM cost (no ledger rows in a window)",
    }
    not_measured = dict(ALWAYS_NOT_MEASURED)
    for key, label in unread.items():
        if metrics[key]["before"] is None or metrics[key]["after"] is None:
            not_measured[key] = label
    return {
        "change_at": change_at.astimezone(timezone.utc).isoformat(),
        "days": days,
        "windows": {
            "before_days": [d.isoformat() for d in before_days],
            "after_days": [d.isoformat() for d in after_days],
            "excluded_days": sorted(d.isoformat() for d in set(exclude_days)),
            "deploy_day": et_day(change_at).isoformat(),
        },
        "metrics": metrics,
        "not_measured": not_measured,
    }


def load_impact_report(user_id: int, change_at: datetime, days: int = DEFAULT_DAYS,
                       exclude_days: Iterable[date] = ()) -> dict[str, Any]:
    """Read every input for both windows and build the report.

    Raises:
        ImpactReadError: any read failed. An unread window must not look like an empty one.
    """
    from cqc_lem.utilities.db import (
        get_follower_stats,
        get_impact_hourly_counts,
        get_impact_llm_cost_by_day,
        get_impact_post_rows,
    )
    days = max(1, min(int(days), MAX_DAYS))
    start, end = utc_bounds(change_at, days)
    posts = get_impact_post_rows(user_id, start, end)
    counts = get_impact_hourly_counts(user_id, start, end)
    cost = get_impact_llm_cost_by_day(user_id, start.astimezone(ET).date() - timedelta(days=1),
                                      end.astimezone(ET).date() + timedelta(days=1))
    # `get_follower_stats` answers [] on a fault, so an empty list is "no snapshots", reported
    # per-metric as not measured rather than failing the whole report.
    age_days = max(1, (datetime.now(timezone.utc) - start).days + 2)
    followers = get_follower_stats(user_id, days=age_days)
    if posts is None or counts is None or cost is None:
        raise ImpactReadError("an impact report input could not be read")
    return build_impact_report(change_at, days, exclude_days, posts, counts, followers, cost)


def _fmt(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:g}"


def render_impact_text(report: Mapping[str, Any], label: str) -> str:
    """Plain-text rendering for the owner email: numbers only, no names or URLs."""
    lines = [f"Impact report: {label}",
             f"Change at {report['change_at']} (deploy day {report['windows']['deploy_day']} "
             f"excluded); {len(report['windows']['before_days'])} days before, "
             f"{len(report['windows']['after_days'])} after.", ""]
    for name, m in report["metrics"].items():
        lines.append(f"{name}: {_fmt(m['before'])} -> {_fmt(m['after'])} "
                     f"(delta {_fmt(m['delta'])}; n {m['n_before']}/{m['n_after']}) {m['verdict']}")
    if report["not_measured"]:
        lines += ["", "Not measured:"] + [f"- {v}" for v in report["not_measured"].values()]
    return "\n".join(lines)
