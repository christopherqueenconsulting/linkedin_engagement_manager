"""The weekly 5-minute report payload (issue #2632, phase 1a).

`build_weekly_report` is pure: it turns already-read inputs into ONE payload that the endpoint (and
later the Friday email) renders, so the page and the email cannot disagree. `load_weekly_report`
does the reads. An input that could not be read is `None` and is named in `not_measured` — never a
zero that reads as "the week was empty".

The organic rate is the existing `post_stats.third_party_engagement_rate` (own comments excluded).
The week-over-week ICP-share delta is deliberately NOT measured: `post_engagers` is last-write-wins
with no event history, so last week's share cannot be reconstructed.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Mapping, Optional, Sequence

from cqc_lem.utilities.comment_outcomes import summarize_outcomes
from cqc_lem.utilities.db import (
    count_pending_drafts,
    get_assist_activity,
    get_comment_log_times,
    get_comment_outcomes,
    get_follower_stats,
    get_weekly_conversion_counts,
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
    "new_conversations": "new conversations and calls booked (lead events could not be read)",
    "comment_volume": "comments per day by mode (comment or Assist records unreadable)",
    "review_seconds": "seconds per review decision (too few approvals in one sitting)",
    "comment_outcomes": "comment outcome rates (no checked comments)",
}
# Approvals further apart than this are different sittings, not one decision's thinking time.
REVIEW_SITTING_GAP_S = 300
REVIEW_MIN_GAPS = 3
# Always unmeasured in phase 1: named every week so a silence is never read as a zero.
_ALWAYS_NOT_MEASURED = ("icp_share_delta", "saves", "sends")


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


def _day_counts(times: Optional[Sequence[datetime]]) -> Optional[dict]:
    return None if times is None else dict(Counter(t.date().isoformat() for t in times))


def review_seconds_median(approved_at: Optional[Sequence[datetime]]) -> Optional[float]:
    """Median seconds between consecutive approvals made in one sitting.

    No draft-shown timestamp is stored, so the pace of approving is the reading: a gap above
    REVIEW_SITTING_GAP_S starts a new sitting and is dropped, and fewer than REVIEW_MIN_GAPS gaps is
    too thin to report (None, never 0).
    """
    if not approved_at:
        return None
    ordered = sorted(approved_at)
    gaps = [(b - a).total_seconds() for a, b in zip(ordered, ordered[1:])]
    gaps = [g for g in gaps if g <= REVIEW_SITTING_GAP_S]
    return float(median(gaps)) if len(gaps) >= REVIEW_MIN_GAPS else None


def comment_volume(logged: Optional[Sequence[datetime]],
                   assist: Optional[Mapping[str, Sequence[datetime]]],
                   outcome_rows: Optional[Sequence[Mapping[str, Any]]]) -> dict:
    """Comments per day by mode, review pace and outcome rates, each None when unreadable.

    Every comment, Assist included, writes a SUCCESS comment log, so Assist is the `posted_at`
    count and automated is the logged count minus it. Per-day numbers need BOTH reads; one
    missing makes the split unknown rather than a guess.
    """
    logged_by_day = _day_counts(logged)
    assist_by_day = _day_counts(assist["posted_at"]) if assist is not None else None
    per_day = None
    if logged_by_day is not None and assist_by_day is not None:
        per_day = [{"date": d, "assist": assist_by_day.get(d, 0),
                    "automated": max(0, logged_by_day.get(d, 0) - assist_by_day.get(d, 0))}
                   for d in sorted(set(logged_by_day) | set(assist_by_day))]
    summary = summarize_outcomes(outcome_rows) if outcome_rows else None
    return {
        "per_day": per_day,
        "automated_total": sum(r["automated"] for r in per_day) if per_day is not None else None,
        "assist_total": sum(r["assist"] for r in per_day) if per_day is not None else None,
        "review_seconds_median": review_seconds_median(
            assist["approved_at"] if assist is not None else None),
        "outcomes": None if summary is None or not summary["checked"] else {
            "checked": summary["checked"],
            "reply_rate": summary["reply_rate"],
            "author_reply_rate": summary["author_reply_rate"],
            "like_rate": summary["like_rate"],
            "demotion_rate": summary["demotion_rate"],
        },
    }


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
    comment_log_times: Optional[Sequence[datetime]] = None,
    assist_activity: Optional[Mapping[str, Sequence[datetime]]] = None,
    outcome_rows: Optional[Sequence[Mapping[str, Any]]] = None,
    conversions: Optional[Mapping[str, int]] = None,
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
    volume = comment_volume(comment_log_times, assist_activity, outcome_rows)
    if volume["per_day"] is None:
        missing.append("comment_volume")
    if volume["review_seconds_median"] is None:
        missing.append("review_seconds")
    if volume["outcomes"] is None:
        missing.append("comment_outcomes")
    if conversions is None:
        missing.append("new_conversations")
    missing.extend(_ALWAYS_NOT_MEASURED)

    return {
        "conversions": dict(conversions) if conversions is not None else None,
        "comment_volume": volume,
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


def has_activity(report: Mapping[str, Any]) -> bool:
    """True when the week shows anything worth a report: a published post or an engager reading.

    An unreadable posts count (None) is not activity, so a database fault sends nothing.
    """
    nums = report.get("numbers") or {}
    return bool(nums.get("posts_published")) or report.get("icp_share") is not None


def iso_year_week(now: datetime) -> str:
    """The send-claim key, e.g. `2026-W48`."""
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def send_weekly_reports(now: Optional[datetime] = None) -> dict:
    """The Friday beat body: one email per eligible user per ISO week.

    Eligible = `get_active_user_ids` (connected, subscribed, not disabled), `WEEKLY_REPORT_ENABLED`
    on for that user, not opted out, and some activity. The claim is taken before the send and
    released if the send fails, so a retry can finish the week but nothing ever sends twice.
    """
    from cqc_lem.utilities.db import (
        claim_weekly_report_send,
        get_active_user_ids,
        get_user_email,
        get_weekly_report_enabled,
        release_weekly_report_send,
    )
    from cqc_lem.utilities.email import send_weekly_report_email
    from cqc_lem.utilities.flags import WEEKLY_REPORT, flag_enabled
    from cqc_lem.utilities.logger import log_debug, log_info, log_warning

    when = now or datetime.now(timezone.utc)
    week = iso_year_week(when)
    result = {"sent": 0, "skipped": 0, "failed": 0}
    for user_id in get_active_user_ids() or []:
        try:
            if not flag_enabled(WEEKLY_REPORT, user_id) or not get_weekly_report_enabled(user_id):
                result["skipped"] += 1
                continue
            email = get_user_email(user_id)
            report = load_weekly_report(user_id, when)
            if not email or not has_activity(report):
                result["skipped"] += 1
                continue
            if not claim_weekly_report_send(user_id, week):
                result["skipped"] += 1
                continue
            try:
                delivered = send_weekly_report_email(email, report)
            except Exception:
                release_weekly_report_send(user_id, week)
                raise
            if delivered:
                result["sent"] += 1
            else:
                release_weekly_report_send(user_id, week)
                result["failed"] += 1
        except Exception as e:
            result["failed"] += 1
            log_warning("Could not send the weekly report", exc=e, user_id=user_id,
                        task_name="auto_weekly_report")
    if result["sent"]:
        log_info(f"Sent {result['sent']} weekly report(s) for {week}", task_name="auto_weekly_report")
    else:
        log_debug(f"No weekly reports sent for {week}", task_name="auto_weekly_report")
    return result


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
        comment_log_times=get_comment_log_times(user_id, start, end),
        assist_activity=get_assist_activity(user_id, start, end),
        # get_comment_outcomes returns [] on a fault, so an empty list reads as unknown here.
        outcome_rows=get_comment_outcomes(user_id, days=7),
        conversions=get_weekly_conversion_counts(user_id, start, end),
    )
