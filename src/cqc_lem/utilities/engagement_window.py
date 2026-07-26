"""Engagement windows — when engagement fires (issues #547, #554).

Two windows live here: the pre-post warm-up around each scheduled post (#547), and the daily
per-user slot that spreads a fleet-wide beat across a window instead of one minute (#554).

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

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

# The task each marker belongs to. One post dispatches BOTH pre-post tasks, so the marker is keyed
# per task — otherwise the viewer dispatch (written second) would clobber the commenting window's
# eta/window_seconds/clamped and the read-back would describe the wrong task.
PRE_POST_TASK_COMMENTING = "automate_commenting"
PRE_POST_TASK_VIEWER = "automate_profile_viewer_engagement"


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


def _marker_key(post_id: int, task_name: str = PRE_POST_TASK_COMMENTING) -> str:
    return f"{_MARKER_PREFIX}{int(post_id)}:{task_name}"


def _write_marker(post_id: int, task_name: str, fields: dict,
                  increments: Optional[dict] = None) -> None:
    client = shared_redis_client()
    if client is None:
        return
    key = _marker_key(post_id, task_name)
    try:
        if fields:
            client.hset(key, mapping={k: str(v) for k, v in fields.items() if v is not None})
        for field, amount in (increments or {}).items():
            client.hincrby(key, field, int(amount))
        client.expire(key, _MARKER_TTL_SECONDS)
    except Exception as e:
        log_warning("Could not record pre-post engagement marker", exc=e, post_id=post_id)


def record_pre_post_scheduled(post_id: int, user_id: int, window: PrePostWindow,
                              task_name: str = PRE_POST_TASK_COMMENTING) -> None:
    """Mark that the pre-post engagement window was dispatched for this post."""
    log_info(
        f"Pre-post engagement window scheduled for {window.eta.isoformat()} "
        f"({window.duration_seconds}s{', clamped' if window.clamped else ''})",
        post_id=post_id, user_id=user_id, task_name=task_name,
    )
    _write_marker(post_id, task_name, {
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
                            task_name: str = PRE_POST_TASK_COMMENTING) -> None:
    """Mark that no pre-post engagement ran for this post, and why (throttle, inactive user, or a
    window that already closed) — the skip is as important to a report as the run."""
    log_warning(f"Pre-post engagement window skipped — {reason}",
                post_id=post_id, user_id=user_id, task_name=task_name)
    _write_marker(post_id, task_name, {
        "user_id": user_id,
        "status": PRE_POST_STATUS_SKIPPED,
        "task_name": task_name,
        "skip_reason": reason,
    })
    track_pre_post_engagement(post_id, user_id, PRE_POST_STATUS_SKIPPED, task_name=task_name,
                              skip_reason=reason)


def record_pre_post_run(post_id: int, user_id: int, comments: Optional[int],
                        now: Optional[datetime] = None) -> None:
    """Record one completed pre-post engagement pass and the comments it left. The loop re-queues
    itself across the window, so runs/comments ACCUMULATE — the marker answers "pre-post commenting
    ran N times for post X and left M comments"."""
    ran_at = _as_utc(now if now is not None else datetime.now(timezone.utc))
    log_info(f"Pre-post engagement pass complete — {comments} comment(s)",
             post_id=post_id, user_id=user_id, task_name=PRE_POST_TASK_COMMENTING)
    _write_marker(post_id, PRE_POST_TASK_COMMENTING,
                  {"user_id": user_id, "last_run_at": ran_at.isoformat()},
                  {"runs": 1, "comments": max(0, int(comments or 0))})
    track_pre_post_engagement(post_id, user_id, "ran", comments=int(comments or 0),
                              ran_at=ran_at.isoformat())


def get_pre_post_window_stat(post_id: int, task_name: str = PRE_POST_TASK_COMMENTING) -> dict:
    """Read back a post's engagement-window marker for one pre-post task (defaults to feed
    commenting; empty dict when unknown / Redis unavailable)."""
    client = shared_redis_client()
    if client is None:
        return {}
    try:
        raw = client.hgetall(_marker_key(post_id, task_name))
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
                # A marker field is only ever observability data — a corrupt/non-numeric value is
                # surfaced verbatim rather than dropped, so a reader can see what was actually there.
                continue
    stat.setdefault("runs", 0)
    stat.setdefault("comments", 0)
    return stat


# --- Daily fan-out staggering (issue #554) ---------------------------------------------------
#
# A single crontab that hands every active user a 15-minute Selenium loop at the same minute makes
# the whole fleet queue on one lane: `se_engage` drains ~8 users/hour, so at 50 users the last
# "golden hour" run lands ~6 hours after the window it was meant for (docs/scaling-plan.md §3).
# Instead every user gets a stable minute inside a window that opens at an anchor hour in THEIR
# timezone, and the beat runs every STAGGER_TICK_MINUTES to dispatch whoever has come due.

STAGGER_TICK_MINUTES = 15  # beat cadence for the staggered fan-outs; also one slot's width

# Per-fan-out defaults. Each is overridable at runtime with <NAME>_ANCHOR_HOUR /
# <NAME>_WINDOW_MINUTES / <NAME>_ANCHOR_TZ, read at call time so ops can retune without a restart.
STAGGER_GOLDEN_HOUR = ("GOLDEN_HOUR", 9, 180)
STAGGER_APPRECIATION_DM = ("APPRECIATION_DM", 8, 120)
STAGGER_GROUP_ENGAGEMENT = ("GROUP_ENGAGEMENT", 12, 120)

_SLOT_CLAIM_PREFIX = "engagement:slot:"
# The claim key carries the slot's local DATE, so "once per user per day" holds no matter when the
# claim was written: a late catch-up (beat or 429 breaker down until the evening) claims THAT day's
# key and leaves tomorrow's untouched. The TTL is then only garbage collection — it just has to
# outlast the rest of the day it was claimed on.
_SLOT_CLAIM_TTL_SECONDS = 26 * 60 * 60

_MINUTES_PER_DAY = 24 * 60


@dataclass(frozen=True)
class StaggerConfig:
    """Where a fan-out's window opens, how wide it is, and whose clock it opens on."""

    name: str
    anchor_hour: int
    window_minutes: int
    local: bool  # anchor in each user's own timezone (True) or in UTC (False)


@dataclass(frozen=True)
class DailySlot:
    """One user's dispatch minute for one fan-out, today."""

    at: datetime           # the slot in UTC (logging / observability)
    local_at: datetime     # the same minute on the anchor clock
    offset_minutes: int    # spread from the anchor hour
    reached: bool          # the slot has arrived (stays True for the rest of the local day)
    in_tick_window: bool   # ...and this is the first beat tick after it


def _env_int(key: str, default: int, low: int, high: int) -> int:
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log_warning(f"{key} ignored (not an integer: {raw!r})")
        return default
    if not low <= value <= high:
        log_warning(f"{key} ignored (outside {low}-{high}: {value})")
        return default
    return value


def stagger_config(fanout: Tuple[str, int, int]) -> StaggerConfig:
    """Resolve one of the STAGGER_* fan-out defaults against the environment."""
    name, anchor_hour, window_minutes = fanout
    return StaggerConfig(
        name=name,
        anchor_hour=_env_int(f"{name}_ANCHOR_HOUR", anchor_hour, 0, 23),
        # A 1-minute window is the escape hatch: every user lands on the anchor minute, i.e. the
        # pre-#554 "everyone at once" behavior, without a code change.
        window_minutes=_env_int(f"{name}_WINDOW_MINUTES", window_minutes, 1, _MINUTES_PER_DAY),
        local=(os.environ.get(f"{name}_ANCHOR_TZ") or "local").strip().lower() != "utc",
    )


def stagger_offset_minutes(user_id: int, window_minutes: int, salt: str = "") -> int:
    """A user's stable minute inside the window. Hashed (not user_id % window) so ids that were
    created together don't clump, and salted per fan-out so the same user isn't first — or last —
    in every window of the day."""
    window = max(1, int(window_minutes))
    digest = hashlib.sha256(f"{salt}:{int(user_id)}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % window


def plan_daily_slot(user_id: int, config: StaggerConfig, tz_name: Optional[str] = None,
                    now: Optional[datetime] = None,
                    tick_minutes: int = STAGGER_TICK_MINUTES) -> DailySlot:
    """Today's dispatch slot for this user/fan-out, and whether it is due now.

    The comparison is pure wall clock on the anchor timezone, so a user keeps the same local
    minute across DST changes, and a window that runs past midnight wraps into the small hours
    rather than falling off the end of the day (which would never fire).
    """
    now_utc = _as_utc(now if now is not None else datetime.now(timezone.utc))
    zone = _zone(tz_name) if config.local else timezone.utc
    local_now = now_utc.astimezone(zone)

    offset = stagger_offset_minutes(user_id, config.window_minutes, config.name)
    minute_of_day = (config.anchor_hour * 60 + offset) % _MINUTES_PER_DAY
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    slot_local = midnight + timedelta(minutes=minute_of_day)

    elapsed = (local_now.replace(tzinfo=None) - slot_local.replace(tzinfo=None)).total_seconds() / 60
    return DailySlot(
        at=slot_local.astimezone(timezone.utc),
        local_at=slot_local,
        offset_minutes=offset,
        reached=elapsed >= 0,
        in_tick_window=0 <= elapsed < max(1, int(tick_minutes)),
    )


def claim_daily_slot(user_id: int, name: str, day: str,
                     ttl_seconds: int = _SLOT_CLAIM_TTL_SECONDS) -> Optional[bool]:
    """Claim one day's slot for this user/fan-out: True when this caller won it, False when it was
    already taken for that day, None when Redis can't answer (the caller then falls back to the
    strict one-tick window instead of dispatching blind).

    `day` is the slot's date on the anchor clock (`DailySlot.local_at`) — keying by it is what
    keeps a late catch-up claim from spilling into the next local day's slot.
    """
    client = shared_redis_client()
    if client is None:
        return None
    try:
        return bool(client.set(f"{_SLOT_CLAIM_PREFIX}{name}:{int(user_id)}:{day}", "1",
                               nx=True, ex=int(ttl_seconds)))
    except Exception as e:
        log_warning("Could not claim daily engagement slot", exc=e, user_id=user_id)
        return None


def _zone(tz_name: Optional[str]):
    if not tz_name:
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        log_warning(f"Unknown timezone {tz_name!r} for engagement slot — anchoring in UTC")
        return timezone.utc
