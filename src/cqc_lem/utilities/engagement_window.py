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
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client
from cqc_lem.utilities.logger import log_info, log_warning
from cqc_lem.utilities.observability import track_pre_post_engagement, track_session_window_shadow

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
    window that already closed) — the skip is as important to a report as the run.
    """
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
    ran N times for post X and left M comments".
    """
    ran_at = _as_utc(now if now is not None else datetime.now(timezone.utc))
    log_info(f"Pre-post engagement pass complete — {comments} comment(s)",
             post_id=post_id, user_id=user_id, task_name=PRE_POST_TASK_COMMENTING)
    _write_marker(post_id, PRE_POST_TASK_COMMENTING,
                  {"user_id": user_id, "last_run_at": ran_at.isoformat()},
                  {"runs": 1, "comments": max(0, int(comments or 0))})
    track_pre_post_engagement(post_id, user_id, "ran", comments=int(comments or 0),
                              ran_at=ran_at.isoformat())


_LANE_CLAIM_PREFIX = "engagement:prepost:claim:"
# Garbage collection only: a post id is never scheduled twice, so the claim just has to outlive the
# window (at most PRE_POST_COMMENT_LEAD_MINUTES) plus any late redelivery of its dispatch.
_LANE_CLAIM_TTL_SECONDS = 24 * 60 * 60

_VIEWER_TOUCH_PREFIX = "engagement:viewer_touch:"
_VIEWER_TOUCH_TTL_SECONDS = 26 * 60 * 60


def claim_pre_post_lane(user_id: int, post_id: int, lane: str,
                        ttl_seconds: int = _LANE_CLAIM_TTL_SECONDS) -> bool:
    """Claim one pre-post lane for one post, so the window runs each lane at most once (issue #2093).

    True when this caller may run the lane, False when a run of it already happened for this post.
    Each lane used to self-requeue every `future_forward` seconds across its window, and every pass
    opened a fresh LinkedIn session — a new session every ~85 s, 41 in one 30-minute bucket. The
    window now runs each lane at most ONCE per post, and this claim is what makes a second dispatch
    (a stale requeue, a broker redelivery of the eta task) a no-op before any session opens.

    Fails OPEN like every other marker here: with no Redis the dispatch runs, as before.
    """
    client = shared_redis_client()
    if client is None:
        return True
    try:
        return bool(client.set(f"{_LANE_CLAIM_PREFIX}{int(user_id)}:{int(post_id)}:{lane}", "1",
                               nx=True, ex=int(ttl_seconds)))
    except Exception as e:
        log_warning("Could not claim pre-post engagement lane", exc=e, user_id=user_id,
                    post_id=post_id, task_name=lane)
        return True


def claim_profile_viewer_touch(user_id: int, viewer_url: str, day: Optional[str] = None,
                               ttl_seconds: int = _VIEWER_TOUCH_TTL_SECONDS) -> bool:
    """Claim today's one touch of a profile viewer, before any session opens (issue #2093).

    True when this caller may dispatch the engagement, False when the viewer was already dispatched
    today. `has_engaged_url_with_x_days` only counts a SUCCESS row, written after the attempt finishes, so
    an in-flight or failed engagement never read as "touched" — viewers were revisited 5-9 times in
    one window. The claim is taken at dispatch, before `engage_with_profile_viewer` opens a session.

    Fails OPEN: with no Redis the dispatch goes ahead and the task's own DB check still applies.
    """
    client = shared_redis_client()
    if client is None:
        return True
    day = day or datetime.now(timezone.utc).date().isoformat()
    digest = hashlib.sha256(viewer_url.strip().rstrip("/").encode("utf-8")).hexdigest()[:24]
    try:
        return bool(client.set(f"{_VIEWER_TOUCH_PREFIX}{int(user_id)}:{day}:{digest}", "1",
                               nx=True, ex=int(ttl_seconds)))
    except Exception as e:
        log_warning("Could not claim profile viewer touch", exc=e, user_id=user_id)
        return True


def get_pre_post_window_stat(post_id: int, task_name: str = PRE_POST_TASK_COMMENTING) -> dict:
    """Read back a post's engagement-window marker for one pre-post task (defaults to feed
    commenting; empty dict when unknown / Redis unavailable).
    """
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

    stat: dict[str, Any] = {}
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
#
# Opening a window at the hour the old single crontab fired does NOT leave the batch where it was —
# it moves the batch's CENTRE OF MASS half a window later, and its TAIL a full window later. That is
# harmless for a fan-out whose lane carries nothing else, and it is exactly what made `se_outreach`
# worse under #554 (issue #696): the appreciation-DM tail slid into the window the post-anchored
# `profile_viewer_engagement` starts arriving in, on the same lane. So a fan-out that SHARES a lane
# with a tighter-tolerance job opens its window `window_minutes / 2` EARLY, which puts the staggered
# batch's midpoint back on the unstaggered batch's hour and leaves the drain time it used to have.
# `APPRECIATION_DM_MIDPOINT_HOUR` below is the invariant that keeps the two numbers in step.
STAGGER_GOLDEN_HOUR = ("GOLDEN_HOUR", 9, 180)
STAGGER_APPRECIATION_DM = ("APPRECIATION_DM", 7, 120)
STAGGER_GROUP_ENGAGEMENT = ("GROUP_ENGAGEMENT", 12, 120)
# Company-page invites (issue #732): a daily drip on the user's OWN clock, replacing the fixed
# 05:00-UTC monthly blast. It opens after the appreciation-DM window has drained (07:00-09:00) so
# the two don't queue on `se_outreach` together, and it carries no deadline of its own — a page
# invite is equally good at 10:00 or 12:30, which is exactly why it gets the wide window.
STAGGER_COMPANY_INVITE = ("COMPANY_INVITE", 10, 180)

# Where the appreciation-DM batch's midpoint has to land: the 08:00 off-peak hour PR #607 approved,
# and the hour the pre-#554 `send-appreciation-dms` crontab fired. It is the send time users
# actually experience on average — the anchor is only where the window OPENS.
APPRECIATION_DM_MIDPOINT_HOUR = 8

# Fan-outs whose window must stay CENTRED on a given hour because they share a lane with a
# tighter-tolerance job. The constants above are pinned to this by a unit test, but the SHIPPED
# retune path is the env vars — and both of #696's failure modes live there, silently: a deployment
# that still pins the pre-#696 `APPRECIATION_DM_ANCHOR_HOUR=8` never gets this fix at all, and
# widening `APPRECIATION_DM_WINDOW_MINUTES` without pulling the anchor back re-creates the exact
# collision. Neither one fails; the batch just drifts back over `profile_viewer_engagement`. So the
# resolved pair is checked at config time and says so out loud.
PINNED_MIDPOINT_HOURS = {"APPRECIATION_DM": APPRECIATION_DM_MIDPOINT_HOUR}

# One warning per distinct resolved pair per process — `stagger_config` runs once per user per beat
# tick, and a per-call warning would be thousands of identical lines a day instead of a signal.
_MIDPOINT_DRIFT_WARNED: set = set()

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


def _warn_if_midpoint_drifted(name: str, anchor_hour: int, window_minutes: int) -> None:
    """Log once when a pinned fan-out's RESOLVED anchor/window no longer centre on their hour."""
    pinned_hour = PINNED_MIDPOINT_HOURS.get(name)
    if pinned_hour is None:
        return
    midpoint = (anchor_hour * 60 + window_minutes / 2) % _MINUTES_PER_DAY
    # Circular difference — a window that wraps past midnight is still near a small pinned hour.
    drift = (midpoint - pinned_hour * 60 + _MINUTES_PER_DAY / 2) % _MINUTES_PER_DAY - _MINUTES_PER_DAY / 2
    if abs(drift) <= STAGGER_TICK_MINUTES:  # under one beat tick is not a drift worth a line
        return
    key = (name, anchor_hour, window_minutes)
    if key in _MIDPOINT_DRIFT_WARNED:
        return
    _MIDPOINT_DRIFT_WARNED.add(key)
    log_warning(
        f"{name} window is off its pinned midpoint by {drift:+.0f} min: "
        f"{name}_ANCHOR_HOUR={anchor_hour} + {name}_WINDOW_MINUTES={window_minutes} centres the batch "
        f"on {int(midpoint) // 60:02d}:{int(midpoint) % 60:02d}, not {pinned_hour:02d}:00 (issue #696). "
        f"Set {name}_ANCHOR_HOUR so anchor*60 + window/2 == {pinned_hour * 60}."
    )


def stagger_config(fanout: Tuple[str, int, int]) -> StaggerConfig:
    """Resolve one of the STAGGER_* fan-out defaults against the environment."""
    name, anchor_hour, window_minutes = fanout
    resolved_anchor = _env_int(f"{name}_ANCHOR_HOUR", anchor_hour, 0, 23)
    # A 1-minute window is the escape hatch: every user lands on the anchor minute, i.e. the
    # pre-#554 "everyone at once" behavior, without a code change.
    resolved_window = _env_int(f"{name}_WINDOW_MINUTES", window_minutes, 1, _MINUTES_PER_DAY)
    _warn_if_midpoint_drifted(name, resolved_anchor, resolved_window)
    return StaggerConfig(
        name=name,
        anchor_hour=resolved_anchor,
        window_minutes=resolved_window,
        local=(os.environ.get(f"{name}_ANCHOR_TZ") or "local").strip().lower() != "utc",
    )


def stagger_offset_minutes(user_id: int, window_minutes: int, salt: str = "") -> int:
    """A user's stable minute inside the window. Hashed (not user_id % window) so ids that were
    created together don't clump, and salted per fan-out so the same user isn't first — or last —
    in every window of the day.
    """
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


# --- Session window gate (issue #2628: off | shadow | enforce) --------------------------------
#
# One answer to "may a BACKGROUND browser session open for this user right now": weekday daytime in
# the user's own timezone, a bounded number of sessions a local day. `shadow` counts and logs what
# enforcement would defer and never refuses. `enforce` raises `SessionDeferred` at the session seam
# (`selenium_util.get_docker_driver`); the Celery task base re-queues the task with an eta at the
# next window start, so work is deferred, never dropped.

SESSION_WINDOW_MODE_ENV = "SESSION_WINDOW_MODE"
SESSION_WINDOW_OFF = "off"
SESSION_WINDOW_SHADOW = "shadow"
SESSION_WINDOW_ENFORCE = "enforce"
SESSION_DAILY_TARGET = 3
SESSION_DAILY_CEILING = 4
FALLBACK_TIMEZONE = "America/New_York"  # the users.timezone default — NOT UTC

# Local-clock block starts in minutes since midnight. A and C are the default windows; the third
# and fourth blocks only open when the day's approved comment volume needs them.
SESSION_BLOCK_STARTS = (("A", 8 * 60), ("C", 12 * 60 + 30), ("D", 16 * 60), ("E", 10 * 60))
SESSION_WINDOWS = {"A": (8 * 60, 8 * 60 + 45), "C": (12 * 60 + 30, 13 * 60 + 15)}
SESSION_BLOCK_MIN_MINUTES = 45
SESSION_BLOCK_MAX_MINUTES = 60
SECONDS_PER_COMMENT = 88  # measured: one approved comment typed per ~88 s
PRE_POST_WINDOW_LEAD_MINUTES = 15  # window B: 15 min before a scheduled post ...
PRE_POST_WINDOW_TAIL_MINUTES = 60  # ... to 60 min after it
SESSION_BAN_START_HOUR = 19
SESSION_BAN_END_HOUR = 7

SESSION_REASON_EXEMPT = "user_started"
SESSION_REASON_WEEKEND = "weekend"
SESSION_REASON_NIGHT = "night"
SESSION_REASON_OUTSIDE_WINDOW = "outside_window"
SESSION_REASON_OVER_TARGET = "over_daily_target"

_SESSION_COUNT_PREFIX = "engagement:sessions:"
_SESSION_COUNT_TTL_SECONDS = 30 * 60 * 60
_TZ_WARNED: set = set()


@dataclass(frozen=True)
class SessionWindowDecision:
    """Whether a background session may open now, and why not."""

    allowed: bool
    reason: str
    window: Optional[str]
    local_now: datetime
    day: str  # the user's local date, the counter's key


class SessionDeferred(BaseException):
    """Raised at the session seam in enforce mode: this background session may not open now.

    Carries the UTC instant of the next window start; the Celery task base re-queues the task
    there instead of failing it. A `BaseException` on purpose: lanes wrap session opening in
    `except Exception` and would log a designed deferral as a failure. Only the task base and the
    content-generation profile fallbacks catch it.
    """

    def __init__(self, user_id: int, lane: str, reason: str, eta: datetime) -> None:
        """Record which session was deferred, why, and the UTC instant it should re-run."""
        super().__init__(f"Session '{lane}' deferred ({reason}) until {eta.isoformat()}")
        self.user_id = user_id
        self.lane = lane
        self.reason = reason
        self.eta = eta


def session_window_mode() -> str:
    """`off` (default), `shadow` or `enforce`. Anything unrecognised is `off`."""
    raw = (os.environ.get(SESSION_WINDOW_MODE_ENV) or SESSION_WINDOW_OFF).strip().lower()
    return raw if raw in (SESSION_WINDOW_SHADOW, SESSION_WINDOW_ENFORCE) else SESSION_WINDOW_OFF


def resolve_session_zone(tz_name: Optional[str]) -> ZoneInfo:
    """The user's zone, falling back to America/New_York (never UTC); warns once per bad value."""
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            if tz_name not in _TZ_WARNED:
                _TZ_WARNED.add(tz_name)
                log_warning("Unusable users.timezone for the session window; using the default zone")
    return ZoneInfo(FALLBACK_TIMEZONE)


def session_blocks(approved_comments: int = 0) -> dict:
    """Window name -> (start, end) minutes since local midnight for a day with this comment volume.

    With no approved comments this is exactly A and C. Otherwise the typing time (~88 s a comment)
    is spread over 2 to 4 blocks of 45 to 60 minutes: 100 comments is about 3 x 50 min.
    """
    if approved_comments <= 0:
        return dict(SESSION_WINDOWS)
    total_minutes = approved_comments * SECONDS_PER_COMMENT / 60
    count = min(SESSION_DAILY_CEILING, max(2, math.ceil(total_minutes / SESSION_BLOCK_MAX_MINUTES)))
    length = min(SESSION_BLOCK_MAX_MINUTES,
                 max(SESSION_BLOCK_MIN_MINUTES, math.ceil(total_minutes / count)))
    return {name: (start, start + length) for name, start in SESSION_BLOCK_STARTS[:count]}


def _session_cap(blocks: dict) -> int:
    return min(SESSION_DAILY_CEILING, max(SESSION_DAILY_TARGET, len(blocks)))


def _post_windows(local_day, post_times: Tuple[datetime, ...], zone: ZoneInfo) -> list:
    """Window B ranges (minutes since local midnight) for the posts scheduled on `local_day`."""
    ranges = []
    for post_time in post_times:
        local_post = _as_utc(post_time).astimezone(zone)
        if local_post.date() != local_day:
            continue
        minute = local_post.hour * 60 + local_post.minute
        ranges.append((minute - PRE_POST_WINDOW_LEAD_MINUTES, minute + PRE_POST_WINDOW_TAIL_MINUTES))
    return ranges


def session_window_decision(user_id: int, lane: str, now: Optional[datetime] = None,
                            tz: Optional[str] = None, sessions_today: int = 0,
                            user_started: bool = False, approved_comments: int = 0,
                            post_times: Tuple[datetime, ...] = ()) -> SessionWindowDecision:
    """Pure decision: may a background `lane` session open for `user_id` at `now`?

    `sessions_today` is how many sessions already opened on the user's local day. Dispatch is
    decided on the user's wall clock (zoneinfo), so DST shifts move the UTC instant, never the hour.
    `user_started` is the explicit exemption flag its entry point passes; the lane name is never
    matched. Window B (`post_times`, UTC) opens 15 min before to 60 min after a scheduled post.
    """
    zone = resolve_session_zone(tz)
    local_now = _as_utc(now if now is not None else datetime.now(timezone.utc)).astimezone(zone)
    day = local_now.date().isoformat()

    def decide(allowed: bool, reason: str, window: Optional[str] = None) -> SessionWindowDecision:
        return SessionWindowDecision(allowed, reason, window, local_now, day)

    if user_started:
        return decide(True, SESSION_REASON_EXEMPT)
    if local_now.weekday() >= 5:
        return decide(False, SESSION_REASON_WEEKEND)
    if local_now.hour >= SESSION_BAN_START_HOUR or local_now.hour < SESSION_BAN_END_HOUR:
        return decide(False, SESSION_REASON_NIGHT)
    minute = local_now.hour * 60 + local_now.minute
    blocks = session_blocks(approved_comments)
    window = next((n for n, (start, end) in blocks.items() if start <= minute < end), None)
    if window is None and any(start <= minute < end for start, end in
                              _post_windows(local_now.date(), post_times, zone)):
        window = "B"
    if window is None:
        return decide(False, SESSION_REASON_OUTSIDE_WINDOW)
    if sessions_today >= _session_cap(blocks):
        return decide(False, SESSION_REASON_OVER_TARGET, window)
    return decide(True, "in_window", window)


def _local_minutes_to_utc(day, minutes: int, zone: ZoneInfo) -> datetime:
    local = datetime(day.year, day.month, day.day, minutes // 60, minutes % 60, tzinfo=zone)
    return local.astimezone(timezone.utc)


def next_window_start(now: datetime, tz: Optional[str] = None, approved_comments: int = 0,
                      sessions_today: int = 0) -> datetime:
    """The UTC instant a deferred session should re-run: the next block start on a weekday.

    A day already at its session cap is skipped, so a deferral never lands in a window that would
    refuse it; otherwise the next weekday's earliest block (window A).
    """
    zone = resolve_session_zone(tz)
    local_now = _as_utc(now).astimezone(zone)
    blocks = session_blocks(approved_comments)
    starts = sorted(start for start, _ in blocks.values())
    day = local_now.date()
    if local_now.weekday() < 5 and sessions_today < _session_cap(blocks):
        minute = local_now.hour * 60 + local_now.minute
        later = [start for start in starts if start > minute]
        if later:
            return _local_minutes_to_utc(day, later[0], zone)
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return _local_minutes_to_utc(day, starts[0], zone)


def _session_key(user_id: int, day: str) -> str:
    return f"{_SESSION_COUNT_PREFIX}{int(user_id)}:{day}"


def peek_session_count(user_id: int, day: str) -> Optional[int]:
    """Sessions already counted on local `day`, or None when Redis can't answer (fails OPEN)."""
    client = shared_redis_client()
    if client is None:
        return None
    try:
        return int(client.get(_session_key(user_id, day)) or 0)
    except Exception as e:
        log_warning("Could not read the session count for the window gate", exc=e, user_id=user_id)
        return None


def count_session(user_id: int, day: str) -> Optional[int]:
    """Count one session for the user on local `day`.

    Returns the new count, or None when Redis can't answer (fails OPEN — the caller then treats
    the session as the first).
    """
    client = shared_redis_client()
    if client is None:
        return None
    key = _session_key(user_id, day)
    try:
        count = int(client.incr(key))
        if count == 1:
            client.expire(key, _SESSION_COUNT_TTL_SECONDS)
        return count
    except Exception as e:
        log_warning("Could not count the session for the window gate", exc=e, user_id=user_id)
        return None


def observe_session_window(user_id: Optional[int], lane: str, now: Optional[datetime] = None,
                           user_started: bool = False) -> Optional[SessionWindowDecision]:
    """The hook at the one browser-session seam, `selenium_util.get_docker_driver`.

    Every `get_driver_wait_pair` lane reaches that seam. `shadow` counts the session and logs/emits
    what enforcement WOULD defer; it never refuses. `enforce` raises `SessionDeferred` for a
    session outside the window; a fault reading state fails OPEN. `off` returns None untouched.

    Raises:
        SessionDeferred: In enforce mode, when a background session may not open now.
    """
    mode = session_window_mode()
    if user_id is None or mode == SESSION_WINDOW_OFF:
        return None
    try:
        from cqc_lem.utilities.db import (
            get_approved_comment_candidates,
            get_scheduled_post_times,
            get_user_timezone,
        )
        tz = get_user_timezone(user_id)
        current = _as_utc(now if now is not None else datetime.now(timezone.utc))
        day = current.astimezone(resolve_session_zone(tz)).date().isoformat()
        enforce = mode == SESSION_WINDOW_ENFORCE
        approved, post_times, prior = 0, (), 0
        if not user_started:
            if enforce:
                approved = len(get_approved_comment_candidates(user_id) or [])
                post_times = tuple(get_scheduled_post_times(
                    user_id, current - timedelta(days=1), current + timedelta(days=2)))
                prior = peek_session_count(user_id, day) or 0
            else:
                counted = count_session(user_id, day)
                prior = (counted - 1) if counted else 0
        decision = session_window_decision(user_id, lane, now=current, tz=tz, sessions_today=prior,
                                           user_started=user_started, approved_comments=approved,
                                           post_times=post_times)
        if decision.allowed:
            if enforce and not user_started:
                count_session(user_id, day)
        elif enforce:
            eta = next_window_start(current, tz, approved, prior)
            log_info(f"Session window enforce: deferring '{lane}' ({decision.reason})",
                     user_id=user_id)
            track_session_window_shadow(user_id, decision.reason, lane, "deferred")
            raise SessionDeferred(user_id, lane, decision.reason, eta)
        else:
            log_info(f"Session window shadow: would defer '{lane}' ({decision.reason})",
                     user_id=user_id)
            track_session_window_shadow(user_id, decision.reason, lane, decision.window or "none")
        return decision
    except SessionDeferred:
        raise
    except Exception as e:  # a fault reading state must never cost a session
        log_warning("Session window check failed", exc=e, user_id=user_id)
        return None
