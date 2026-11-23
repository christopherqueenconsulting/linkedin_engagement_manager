"""The weekly 5-minute report payload (issue #2632, phase 1a).

`build_weekly_report` is pure: it turns already-read inputs into ONE payload that the endpoint (and
later the Friday email) renders, so the page and the email cannot disagree. `load_weekly_report`
does the reads. An input that could not be read is `None` and is named in `not_measured` — never a
zero that reads as "the week was empty".

The organic rate is the existing `post_stats.third_party_engagement_rate` (own comments excluded).
The week-over-week ICP-share delta is deliberately NOT measured: `post_engagers` is last-write-wins
with no event history, so last week's share cannot be reconstructed.
"""

from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Mapping, Optional, Sequence

from cqc_lem.utilities.db import (
    count_pending_drafts,
    get_follower_stats,
    get_weekly_engager_icp_scores,
    get_weekly_post_rows,
)
from cqc_lem.utilities.post_stats import third_party_engagement_rate

MAX_ACTIONS = 3
# An engager counts as inside the ICP at this `icp_fit` score (ICP_UNKNOWN is 50, so unknown never
# qualifies); no `leads` row = no reading.
ICP_FIT_MIN = 60
_FOLLOWER_LOOKBACK_DAYS = 21

# The authenticated pages the actions point at (no tokenized unauthenticated links).
LINK_REVIEW_DRAFTS = "/content"
LINK_ACCOUNT = "/account"

NOT_MEASURED_LABELS = {
    "posts_published": "posts published (could not read posts)",
    "median_impressions": "median impressions (no post had impressions captured)",
    "organic_engagement_rate": "organic engagement rate (own-comment count or impressions missing)",
    "follower_delta": "follower change (no comparable follower snapshots)",
    "icp_share": "ICP share of engagers (no engagers with a fit reading)",
    "icp_share_delta": "ICP share week over week (engager history is not kept)",
    "saves": "saves (official analytics read not enabled)",
    "sends": "sends (official analytics read not enabled)",
    "new_conversations": "new conversations (reply detection not trustworthy yet)",
    "calls_booked": "calls booked (no meeting stage yet)",
}
# Always unmeasured in phase 1: named every week so a silence is never read as a zero.
_ALWAYS_NOT_MEASURED = ("icp_share_delta", "saves", "sends", "new_conversations", "calls_booked")


def _week_numbers(rows: Optional[Sequence[Mapping[str, Any]]]) -> dict:
    """posts_published, median_impressions and organic_engagement_rate for one window."""
    if rows is None:
        return {"posts_published": None, "median_impressions": None,
                "organic_engagement_rate": None}
    impressions = [int(r["impressions"]) for r in rows if r.get("impressions")]
    pooled = [0, 0]  # (organic engagement score, impressions) over posts with a full reading
    for r in rows:
        rate = third_party_engagement_rate(r.get("reactions"), r.get("comments"),
                                           r.get("own_comments"), r.get("reposts"),
                                           r.get("impressions"))
        if rate is not None:
            pooled[0] += rate * int(r["impressions"])
            pooled[1] += int(r["impressions"])
    return {
        "posts_published": len(rows),
        "median_impressions": median(impressions) if impressions else None,
        "organic_engagement_rate": pooled[0] / pooled[1] if pooled[1] else None,
    }


def _delta(now_value: Optional[float], before: Optional[float]) -> Optional[float]:
    return None if now_value is None or before is None else now_value - before


def follower_delta(snapshots: Optional[Sequence[Mapping[str, Any]]],
                   week_start: datetime) -> Optional[int]:
    """Latest follower count minus the latest one captured at or before `week_start`.

    `snapshots` is `get_follower_stats` output (newest first). None if either end is NULL or absent.
    """
    if not snapshots:
        return None
    latest = snapshots[0].get("follower_count")
    cutoff = week_start.replace(tzinfo=None) if week_start.tzinfo is None else \
        week_start.astimezone(timezone.utc).replace(tzinfo=None)
    base = next((s.get("follower_count") for s in snapshots
                 if s.get("captured_at") is not None and s["captured_at"] <= cutoff), None)
    if latest is None or base is None:
        return None
    return int(latest) - int(base)


def icp_share(scores: Optional[Sequence[Optional[int]]]) -> Optional[dict]:
    """Share of engagers with a fit reading who are inside the ICP band, with n shown.

    Engagers without a `leads` row are excluded from the denominator (unmeasured, not outside).
    """
    if not scores:
        return None
    read = [s for s in scores if s is not None]
    if not read:
        return None
    inside = sum(1 for s in read if s >= ICP_FIT_MIN)
    return {"share": inside / len(read), "inside": inside, "n": len(read),
            "engagers": len(scores)}


def _actions(pending_drafts: Optional[int], posts_published: Optional[int],
             organic_rate: Optional[float], icp: Optional[dict]) -> list:
    """At most MAX_ACTIONS rule-based actions; the pending-drafts one always leads."""
    actions = []
    if pending_drafts is None:
        actions.append({"key": "pending_drafts", "text": "Check your draft queue: it could not be read",
                        "link": LINK_REVIEW_DRAFTS})
    elif pending_drafts > 0:
        noun = "draft" if pending_drafts == 1 else "drafts"
        actions.append({"key": "pending_drafts", "text": f"{pending_drafts} {noun} waiting: review",
                        "link": LINK_REVIEW_DRAFTS})
    else:
        actions.append({"key": "pending_drafts", "text": "No drafts waiting: queue next week's posts",
                        "link": LINK_REVIEW_DRAFTS})
    if posts_published == 0:
        actions.append({"key": "no_posts", "text": "Nothing published this week: check the schedule",
                        "link": LINK_REVIEW_DRAFTS})
    if icp is not None and icp["share"] < 0.25:
        actions.append({"key": "icp_gap", "text": "Few engagers fit your ICP: review your targeting",
                        "link": LINK_ACCOUNT})
    if organic_rate is None and posts_published:
        actions.append({"key": "organic_unreadable",
                        "text": "Organic rate unreadable: check stats capture", "link": LINK_ACCOUNT})
    return actions[:MAX_ACTIONS]


def build_weekly_report(
    *, week_start: datetime, week_end: datetime,
    this_week_rows: Optional[Sequence[Mapping[str, Any]]],
    last_week_rows: Optional[Sequence[Mapping[str, Any]]],
    follower_snapshots: Optional[Sequence[Mapping[str, Any]]],
    engager_icp_scores: Optional[Sequence[Optional[int]]],
    pending_drafts: Optional[int],
) -> dict:
    """The report payload. Pure: no I/O, every unreadable input is None and listed in `not_measured`."""
    this_week = _week_numbers(this_week_rows)
    last_week = _week_numbers(last_week_rows)
    deltas = {k: _delta(this_week[k], last_week[k]) for k in this_week}
    followers = follower_delta(follower_snapshots, week_start)
    icp = icp_share(engager_icp_scores)

    missing = [k for k in ("posts_published", "median_impressions", "organic_engagement_rate")
               if this_week[k] is None]
    if followers is None:
        missing.append("follower_delta")
    if icp is None:
        missing.append("icp_share")
    missing.extend(_ALWAYS_NOT_MEASURED)

    return {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "numbers": {**this_week, "follower_delta": followers},
        "last_week": last_week,
        "deltas": deltas,
        "icp_share": icp,
        "actions": _actions(pending_drafts, this_week["posts_published"],
                            this_week["organic_engagement_rate"], icp),
        "not_measured": [{"key": k, "label": NOT_MEASURED_LABELS[k]} for k in missing],
    }


def load_weekly_report(user_id: int, now: Optional[datetime] = None) -> dict:
    """Read the inputs for the 7 days ending `now` and build the payload."""
    end = now or datetime.now(timezone.utc)
    start = end - timedelta(days=7)
    return build_weekly_report(
        week_start=start, week_end=end,
        this_week_rows=get_weekly_post_rows(user_id, start, end),
        last_week_rows=get_weekly_post_rows(user_id, start - timedelta(days=7), start),
        follower_snapshots=get_follower_stats(user_id, days=_FOLLOWER_LOOKBACK_DAYS),
        engager_icp_scores=get_weekly_engager_icp_scores(user_id, start, end),
        pending_drafts=count_pending_drafts(user_id),
    )
