"""The ICP-audience report: who engages with a user's posts versus who they want (issue #2667).

Delivered in-app only, on demand, over the last `MONTHS` calendar months. `build_icp_audience_report`
is pure; `load_icp_audience_report` does the reads, every one scoped to the caller's `user_id`, so
the payload carries nothing from another user.

Every row shows its `n`. Below `MIN_N` a row is `insufficient` and carries no share, so a thin month
never reads as a trend. Titles are not stored for engagers, so they are named in `not_measured`
rather than guessed. `post_engagers` is last-write-wins, so a person counts in the month of their
MOST RECENT engagement, never an event history.
"""

from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from cqc_lem.utilities.weekly_report import ICP_FIT_MIN

MIN_N = 5
MONTHS = 6
TOP_SEGMENTS = 5

NOT_MEASURED = [
    {"key": "titles", "label": "engager job titles (not stored for engagers)"},
]


def _share(inside: int, n: int) -> Optional[float]:
    return inside / n if n >= MIN_N else None


def _month_key(when: Any) -> Optional[str]:
    return f"{when.year}-{when.month:02d}" if isinstance(when, datetime) else None


def _window_start(now: datetime) -> datetime:
    index = now.year * 12 + now.month - 1 - (MONTHS - 1)
    return datetime(index // 12, index % 12 + 1, 1, tzinfo=timezone.utc)


def build_icp_audience_report(
    rows: Optional[Sequence[Mapping[str, Any]]],
    segment_names: Optional[Mapping[str, str]] = None,
) -> dict:
    """Overall share, top segments and the month-by-month gap. None `rows` = unreadable."""
    if rows is None:
        return {"readable": False, "min_n": MIN_N, "overall": None, "segments": [],
                "gap_by_month": [], "not_measured": NOT_MEASURED}
    names = segment_names or {}
    scored = [r for r in rows if r.get("icp_score") is not None]
    inside = sum(1 for r in scored if r["icp_score"] >= ICP_FIT_MIN)

    by_segment: dict[str, int] = {}
    for r in rows:
        if r.get("segment_id"):
            by_segment[r["segment_id"]] = by_segment.get(r["segment_id"], 0) + 1
    segmented = sum(by_segment.values())
    segments = [
        {"segment_id": sid, "name": names.get(sid) or sid, "n": n,
         "share_of_segmented": _share(n, segmented) if n >= MIN_N else None,
         "insufficient": n < MIN_N}
        for sid, n in sorted(by_segment.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_SEGMENTS]
    ]

    months: dict[str, list[int]] = {}
    for r in scored:
        key = _month_key(r.get("engaged_at"))
        if key:
            bucket = months.setdefault(key, [0, 0])
            bucket[0] += 1
            bucket[1] += 1 if r["icp_score"] >= ICP_FIT_MIN else 0
    gap = []
    for key in sorted(months):
        n, hit = months[key]
        share = _share(hit, n)
        gap.append({"month": key, "n": n, "inside": hit, "share": share,
                    "gap": None if share is None else 1 - share, "insufficient": n < MIN_N})

    return {
        "readable": True,
        "min_n": MIN_N,
        "overall": {"engagers": len(rows), "n": len(scored), "inside": inside,
                    "share": _share(inside, len(scored)), "insufficient": len(scored) < MIN_N},
        "segmented": segmented,
        "segments": segments,
        "gap_by_month": gap,
        "not_measured": NOT_MEASURED,
    }


def load_icp_audience_report(user_id: int, now: Optional[datetime] = None) -> dict:
    """Read the caller's own engager rows for the last MONTHS months and build the report."""
    from cqc_lem.utilities.db import get_engagement_preferences, get_icp_audience_rows
    from cqc_lem.utilities.icp_rubric import parse_icp_definition

    end = now or datetime.now(timezone.utc)
    rows = get_icp_audience_rows(user_id, _window_start(end), end)
    definition = parse_icp_definition((get_engagement_preferences(user_id) or {}).get("icp_definition"))
    names = {s.id: s.name for s in definition.segments} if definition else {}
    return build_icp_audience_report(rows, names)
