"""Pre-post engagement window (issue #547).

The engagement-hacking intent behind the pre-post feed-commenting run is to be *active on the feed
right before your own post publishes*. Two things kept that from being true in practice:

1. `auto_check_scheduled_posts` looks back to "yesterday", so a post picked up at (or after) its
   scheduled time produced an `eta` in the PAST — Celery then fired a "pre"-post warm-up loop that
   ran DURING or AFTER publication. `plan_pre_post_window` clamps that: it fires ASAP (with a
   shortened loop that still ends at the post time) when there is meaningfully less than the full
   lead left, and returns None — don't dispatch at all — once the window has effectively closed.
2. Nothing recorded whether the warm-up actually ran for a given post, so the window could not be
   verified after the fact. The marker helpers below keep a small per-post stat in Redis (the same
   runtime-state store the 429 breaker uses, so it survives deploys) alongside a PostHog event.

Every marker helper fails OPEN: no Redis (or a Redis error) degrades to logging only, never to a
failed dispatch.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client
from cqc_lem.utilities.logger import log_info, log_warning
from cqc_lem.utilities.observability import track_pre_post_engagement

# Lead times the scheduler warms up with, kept here next to the planner that clamps them.
PRE_POST_COMMENT_LEAD_MINUTES = 15
PRE_POST_VIEWER_LEAD_MINUTES = 10

# A warm-up shorter than this isn't engagement — it's one page load that lands on top of (or after)
# the publish itself, so the window is treated as closed instead.
MIN_PRE_POST_WINDOW_SECONDS = 120

_MARKER_PREFIX = "engagement:prepost:"
_MARKER_TTL_SECONDS = 7 * 24 * 60 * 60  # long enough for a weekly report to read it back

PRE_POST_STATUS_SCHEDULED = "scheduled"
PRE_POST_STATUS_SKIPPED = "skipped"

# Skip reasons — stable strings so a report/dashboard can group by them.
PRE_POST_SKIP_PAST_WINDOW = "past_window"
PRE_POST_SKIP_THROTTLED = "throttled"
PRE_POST_SKIP_USER_INACTIVE = "user_inactive"


@dataclass(frozen=True)
class PrePostWindow:
    """When the pre-post engagement loop should start, and how long it may run."""

    eta: datetime
    duration_seconds: int
    clamped: bool


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def plan_pre_post_window(scheduled_time: datetime, lead_minutes: int,
                         now: Optional[datetime] = None,
                         min_window_seconds: int = MIN_PRE_POST_WINDOW_SECONDS) -> Optional[PrePostWindow]:
    """The eta + loop duration for a pre-post engagement task, or None when the window has closed.

    Full lead available → start exactly `lead_minutes` before the post. Less than that (a late
    pickup) → start now and shorten the loop so it still ends at the post time rather than running
    past it. At/after the post time — or too close to warm up meaningfully — → None.
    """
    now = _as_utc(now if now is not None else datetime.now(timezone.utc))
    scheduled_time = _as_utc(scheduled_time)

    lead_seconds = max(0, int(lead_minutes) * 60)
    remaining = (scheduled_time - now).total_seconds()

    if remaining <= max(0, int(min_window_seconds)):
        return None

    if remaining >= lead_seconds:
        return PrePostWindow(eta=scheduled_time - timedelta(seconds=lead_seconds),
                             duration_seconds=lead_seconds, clamped=False)

    return PrePostWindow(eta=now, duration_seconds=int(remaining), clamped=True)


def _marker_key(post_id: int) -> str:
    return f"{_MARKER_PREFIX}{int(post_id)}"


def _write_marker(post_id: int, fields: dict, increments: Optional[dict] = None) -> None:
    client = shared_redis_client()
    if client is None:
        return
    key = _marker_key(post_id)
    try:
        if fields:
            client.hset(key, mapping={k: str(v) for k, v in fields.items() if v is not None})
        for field, amount in (increments or {}).items():
            client.hincrby(key, field, int(amount))
        client.expire(key, _MARKER_TTL_SECONDS)
    except Exception as e:
        log_warning("Could not record pre-post engagement marker", exc=e, post_id=post_id)


def record_pre_post_scheduled(post_id: int, user_id: int, window: PrePostWindow,
                              task_name: str = "automate_commenting") -> None:
    """Mark that the pre-post engagement window was dispatched for this post."""
    log_info(
        f"Pre-post engagement window scheduled for {window.eta.isoformat()} "
        f"({window.duration_seconds}s{', clamped' if window.clamped else ''})",
        post_id=post_id, user_id=user_id, task_name=task_name,
    )
    _write_marker(post_id, {
        "user_id": user_id,
        "status": PRE_POST_STATUS_SCHEDULED,
        "task_name": task_name,
        "eta": window.eta.isoformat(),
        "window_seconds": window.duration_seconds,
        "clamped": int(window.clamped),
    })
    track_pre_post_engagement(post_id, user_id, PRE_POST_STATUS_SCHEDULED, task_name=task_name,
                              eta=window.eta.isoformat(), window_seconds=window.duration_seconds,
                              clamped=window.clamped)


def record_pre_post_skipped(post_id: int, user_id: int, reason: str,
                            task_name: str = "automate_commenting") -> None:
    """Mark that no pre-post engagement ran for this post, and why (throttle, inactive user, or a
    window that already closed) — the skip is as important to a report as the run."""
    log_warning(f"Pre-post engagement window skipped — {reason}",
                post_id=post_id, user_id=user_id, task_name=task_name)
    _write_marker(post_id, {
        "user_id": user_id,
        "status": PRE_POST_STATUS_SKIPPED,
        "task_name": task_name,
        "skip_reason": reason,
    })
    track_pre_post_engagement(post_id, user_id, PRE_POST_STATUS_SKIPPED, task_name=task_name,
                              skip_reason=reason)


def record_pre_post_run(post_id: int, user_id: int, comments: int,
                        now: Optional[datetime] = None) -> None:
    """Record one completed pre-post engagement pass and the comments it left. The loop re-queues
    itself across the window, so runs/comments ACCUMULATE — the marker answers "pre-post commenting
    ran N times for post X and left M comments"."""
    ran_at = _as_utc(now if now is not None else datetime.now(timezone.utc))
    log_info(f"Pre-post engagement pass complete — {comments} comment(s)",
             post_id=post_id, user_id=user_id, task_name="automate_commenting")
    _write_marker(post_id,
                  {"user_id": user_id, "last_run_at": ran_at.isoformat()},
                  {"runs": 1, "comments": max(0, int(comments or 0))})
    track_pre_post_engagement(post_id, user_id, "ran", comments=int(comments or 0),
                              ran_at=ran_at.isoformat())


def get_pre_post_window_stat(post_id: int) -> dict:
    """Read back a post's engagement-window marker (empty dict when unknown / Redis unavailable)."""
    client = shared_redis_client()
    if client is None:
        return {}
    try:
        raw = client.hgetall(_marker_key(post_id))
    except Exception as e:
        log_warning("Could not read pre-post engagement marker", exc=e, post_id=post_id)
        return {}
    if not raw:
        return {}

    stat = {}
    for key, value in raw.items():
        name = key.decode("utf-8", "ignore") if isinstance(key, bytes) else str(key)
        text = value.decode("utf-8", "ignore") if isinstance(value, bytes) else str(value)
        stat[name] = text
    for numeric in ("runs", "comments", "window_seconds", "user_id", "clamped"):
        if numeric in stat:
            try:
                stat[numeric] = int(stat[numeric])
            except ValueError:
                pass
    stat.setdefault("runs", 0)
    stat.setdefault("comments", 0)
    return stat
