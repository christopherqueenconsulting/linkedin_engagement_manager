"""Whether the active users have a usable signed-in LinkedIn session right now (#2356).

The egress reading (`utilities/egress_probe.py`) answers "can the browser's proxy reach
linkedin.com?". A session can be unusable while the egress is fine: `li_at` expired and the
password fallback drew a challenge, a device approval is pending or timed out, the challenge
cooldown is running, the 429 breaker is open, or automation is paused. Each of those is already
recorded in Redis by its single owning module (`login_status.py`, `rate_limit.py`); this module
only READS them, so `/health/deep` can report the session the way it reports the egress.

No browser is opened and no LinkedIn request is made. The reading is cached in-process because
the caller is an unauthenticated endpoint, and nothing it returns names a user, an email or a
timestamp — only a state word and two counts.
"""

import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from cqc_lem.utilities.logger import log_debug, log_warning

# Per-user states, worst first — `summarize` reports the worst one seen.
SESSION_NEEDS_OWNER = "needs_owner"   # approval pending / timed out, or a challenge cooldown running
SESSION_STALE = "stale"               # no sign-in record, or the last sign-in is older than the window
SESSION_BACKING_OFF = "backing_off"   # the global 429 breaker is open, or automation is paused
SESSION_OK = "ok"                     # signed in within the window
SESSION_UNKNOWN = "unknown"           # Redis unreadable, no active users, or the reading crashed

_SEVERITY = (SESSION_NEEDS_OWNER, SESSION_STALE, SESSION_BACKING_OFF, SESSION_OK)
FAILING = frozenset({SESSION_NEEDS_OWNER, SESSION_STALE})

_NEEDS_OWNER_STATES = frozenset({"approval_pending", "approval_timed_out"})

_DEFAULT_STALE_HOURS = 24.0
_DEFAULT_CACHE_SECONDS = 60
_MAX_USERS = 10  # bound the Redis reads one /health/deep call can cause, however many users exist

_CACHE: dict = {"at": 0.0, "value": None}
_CACHE_LOCK = threading.Lock()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _age_seconds(iso: Optional[str], now: datetime) -> Optional[float]:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds()


def classify_user(record: Optional[dict], cooldown_remaining: int, backing_off: bool,
                  stale_hours: Optional[float] = None, now: Optional[datetime] = None) -> str:
    """One user's session state from what the owning modules recorded.

    Args:
        record: `login_status.get_login_status(user_id)` — None when nothing is recorded.
        cooldown_remaining: `login_status.challenge_cooldown_remaining(user_id)`.
        backing_off: Whether the global 429 breaker is open or automation is paused.
        stale_hours: The sign-in age that reads `stale`. Defaults to
            ``LINKEDIN_SESSION_STALE_HOURS`` (24).
        now: The reference time (tests).

    Returns:
        ``needs_owner``, ``stale``, ``backing_off`` or ``ok``, checked in that order.
    """
    state = (record or {}).get("state")
    if state in _NEEDS_OWNER_STATES or cooldown_remaining > 0:
        return SESSION_NEEDS_OWNER
    if stale_hours is None:
        stale_hours = _env_float("LINKEDIN_SESSION_STALE_HOURS", _DEFAULT_STALE_HOURS)
    age = _age_seconds((record or {}).get("signed_in_at"), now or datetime.now(timezone.utc))
    if age is None or age > stale_hours * 3600:
        return SESSION_STALE
    if backing_off:
        return SESSION_BACKING_OFF
    return SESSION_OK


def summarize(states: list) -> str:
    """The single `linkedin_session` reading for per-user states: the worst one, or `unknown`."""
    for state in _SEVERITY:
        if state in states:
            return state
    return SESSION_UNKNOWN


def _unknown() -> dict:
    return {"linkedin_session": SESSION_UNKNOWN, "session_checked": 0, "session_failing": 0}


def _measure() -> dict:
    from cqc_lem.utilities.db import get_active_user_ids
    from cqc_lem.utilities.linkedin import login_status, rate_limit

    # Every reader below fails open to "nothing recorded" without Redis, which would read as
    # `stale` (or worse, `ok` for the breaker). Prove Redis answers first: unmeasured is never ok.
    client = rate_limit.shared_redis_client()
    if client is None:
        log_debug("LinkedIn session health: Redis unavailable")
        return _unknown()
    try:
        client.ping()
    except Exception as e:
        log_debug(f"LinkedIn session health: Redis unreadable ({type(e).__name__})")
        return _unknown()

    user_ids = list(get_active_user_ids() or [])[:_MAX_USERS]
    if not user_ids:
        return _unknown()  # nothing measured — and a DB fault also reads as no rows
    backing_off = (rate_limit.rate_limit_cooldown_remaining() > 0
                   or rate_limit.is_automation_paused())
    stale_hours = _env_float("LINKEDIN_SESSION_STALE_HOURS", _DEFAULT_STALE_HOURS)
    states = [classify_user(login_status.get_login_status(uid),
                            login_status.challenge_cooldown_remaining(uid),
                            backing_off, stale_hours)
              for uid in user_ids]
    return {"linkedin_session": summarize(states), "session_checked": len(states),
            "session_failing": sum(1 for s in states if s in FAILING)}


def session_health(use_cache: bool = True) -> dict:
    """``{"linkedin_session": <state>, "session_checked": n, "session_failing": k}``.

    Args:
        use_cache: Serve a reading younger than ``LINKEDIN_SESSION_HEALTH_CACHE_SECONDS`` (60).

    Returns:
        ``linkedin_session`` is the worst per-user state (see `classify_user`), or ``unknown``
        when Redis is unreadable, no user is active, or the reading crashed.
        ``session_checked`` is how many active users were read (at most 10) and
        ``session_failing`` how many of them are ``needs_owner`` or ``stale``. Never raises.
    """
    ttl = _env_float("LINKEDIN_SESSION_HEALTH_CACHE_SECONDS", _DEFAULT_CACHE_SECONDS)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE["value"]
        if use_cache and cached is not None and now - _CACHE["at"] < ttl:
            return dict(cached)
        try:
            value = _measure()
        except Exception as e:
            # Cached like any other reading, so a persistent fault warns once per window rather
            # than once per scrape.
            log_warning("Deep health check could not measure the LinkedIn session", exc=e)
            value = _unknown()
        _CACHE.update(at=now, value=value)
        return dict(value)


def should_degrade(reading: dict) -> bool:
    """Whether this reading should turn a healthy `/health/deep` into `degraded`.

    Off unless ``HEALTH_DEEP_SESSION_DEGRADES`` is true: the reading is report-only until a week of
    readings shows the stale window does not flap on rest days and nights. Only ``needs_owner``
    and ``stale`` degrade; ``backing_off`` is a pause the breaker or a deploy chose, and ``unknown``
    is unmeasured.
    """
    if os.getenv("HEALTH_DEEP_SESSION_DEGRADES", "false").strip().lower() not in ("1", "true",
                                                                                 "yes", "on"):
        return False
    return reading.get("linkedin_session") in FAILING


def reset_cache() -> None:
    """Forget the cached reading (tests, and an operator who just re-signed in)."""
    with _CACHE_LOCK:
        _CACHE.update(at=0.0, value=None)
