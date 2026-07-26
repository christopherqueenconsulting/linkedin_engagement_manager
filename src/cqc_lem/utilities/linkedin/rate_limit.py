"""Shared circuit breaker for LinkedIn HTTP 429 rate-limiting.

LinkedIn rate-limits by egress IP, so a 429 hit by one engagement task means every
other Selenium task (comments, replies, viewer DMs, appreciation DMs) will also be
throttled. Without coordination each task independently spins up a browser, navigates
to the feed, and re-trips the limit — which prolongs the block. This breaker records
the 429 in Redis with a cooldown TTL so subsequent tasks skip the LinkedIn navigation
until it expires. Fails open: if Redis is unavailable the breaker no-ops and callers
behave as before.
"""

import json
import os
from datetime import datetime, timezone

from cqc_lem.utilities.logger import log_warning

_COOLDOWN_KEY = "linkedin:429_cooldown"
_TRIP_COUNT_KEY = "linkedin:429_trip_count"   # consecutive trips → escalating cooldown
_PAUSE_KEY = "linkedin:automation_paused"     # manual global Selenium pause
_DEFAULT_COOLDOWN_SECONDS = 1800  # 30 min
_DEFAULT_MAX_COOLDOWN_SECONDS = 6 * 60 * 60  # cap the escalation at 6h
# Grace window added to the cooldown when setting the consecutive-trip counter's TTL. The counter is
# what drives escalation; tying its lifetime to (cooldown + grace) instead of a fixed 24h makes the
# escalation SELF-RESETTING: once a full cooldown elapses without a fresh trip (the throttle lifted,
# so the post-cooldown probe either succeeded → clear_rate_limit, or simply didn't re-trip), the
# counter expires and the next 429 starts back at the base cooldown. A fixed 24h TTL let the counter
# outlive several 6h cooldowns, pinning escalation at the cap long after conditions improved.
_DEFAULT_TRIP_COUNT_GRACE_SECONDS = 30 * 60  # 30 min


class LinkedInRateLimited(RuntimeError):
    """LinkedIn is rate-limiting this session (HTTP 429) — back off before retrying.

    Subclasses RuntimeError so existing broad handlers keep treating it as a fatal,
    back-off-worthy login failure. Also raised when automation is manually paused.
    """


def _cooldown_seconds() -> int:
    try:
        return int(os.getenv("LINKEDIN_RATE_LIMIT_COOLDOWN_SECONDS", str(_DEFAULT_COOLDOWN_SECONDS)))
    except ValueError:
        return _DEFAULT_COOLDOWN_SECONDS


def _max_cooldown_seconds() -> int:
    try:
        return int(os.getenv("LINKEDIN_RATE_LIMIT_MAX_COOLDOWN_SECONDS", str(_DEFAULT_MAX_COOLDOWN_SECONDS)))
    except ValueError:
        return _DEFAULT_MAX_COOLDOWN_SECONDS


def _trip_count_grace_seconds() -> int:
    try:
        return int(os.getenv("LINKEDIN_RATE_LIMIT_TRIP_GRACE_SECONDS", str(_DEFAULT_TRIP_COUNT_GRACE_SECONDS)))
    except ValueError:
        return _DEFAULT_TRIP_COUNT_GRACE_SECONDS


def _redis_client():
    """Redis handle for the breaker, or None if unavailable (breaker then no-ops).

    Uses the Celery broker URL when it points at Redis; on AWS the broker is SQS and
    the result backend is Redis, so fall back to that, then to the local default.
    """
    try:
        import redis
    except Exception:
        return None
    url = os.getenv("CELERY_BROKER_URL", "")
    if not url.startswith("redis"):
        url = os.getenv("CELERY_RESULT_BACKEND", "")
    if not url.startswith("redis"):
        url = f"redis://redis:{os.getenv('REDIS_PORT', '6379')}/0"
    try:
        return redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
    except Exception:
        return None


def shared_redis_client():
    """Public handle to the same Redis this breaker uses (None when unavailable), so other
    runtime-state helpers reuse one URL-resolution rule instead of re-deriving it."""
    return _redis_client()


def mark_rate_limited(reason: str = "") -> None:
    client = _redis_client()
    if client is None:
        return
    try:
        # Escalating back-off: each CONSECUTIVE trip doubles the cooldown — base, 2x, 4x, … up to a
        # cap. A fixed 30-min cooldown meant that as soon as it expired some task probed LinkedIn,
        # drew a fresh 429 and re-tripped it every ~30 min forever (the doom loop). Escalation probes
        # less and less often so the throttled IP can actually recover. The consecutive-trip counter
        # is cleared by a successful login (clear_rate_limit) OR self-expires once a full cooldown +
        # grace elapses with no new trip — see the counter-TTL note below.
        try:
            trips = int(client.incr(_TRIP_COUNT_KEY))
        except Exception:
            trips = 1
        seconds = min(_max_cooldown_seconds(), _cooldown_seconds() * (2 ** max(0, trips - 1)))
        # Counter lives exactly as long as this back-off window plus a grace period. If the throttle
        # lifts, the post-cooldown probe won't re-trip within grace, the counter expires, and the
        # NEXT 429 (if any) escalates from scratch — instead of staying pinned at the cap for a day.
        try:
            client.expire(_TRIP_COUNT_KEY, seconds + _trip_count_grace_seconds())
        except Exception:
            # Best-effort TTL refresh — the counter still has its previous TTL, so a failure here
            # just means slightly less-precise self-reset timing, not a broken breaker. Swallow it.
            pass
        client.set(_COOLDOWN_KEY, reason or "429", ex=seconds)
        log_warning(f"LinkedIn 429 circuit breaker OPEN for {seconds}s (consecutive trip #{trips}) "
                    "— Selenium engagement paused", action_type="rate_limit", http_status=429)
    except Exception as e:
        log_warning("Failed to set LinkedIn 429 circuit breaker", exc=e, action_type="rate_limit")


def rate_limit_cooldown_remaining() -> int:
    """Seconds left on the breaker, or 0 if closed / Redis unavailable."""
    client = _redis_client()
    if client is None:
        return 0
    try:
        ttl = client.ttl(_COOLDOWN_KEY)
    except Exception:
        return 0
    return ttl if ttl and ttl > 0 else 0


def clear_rate_limit() -> None:
    """Close the breaker AND reset the consecutive-trip counter — called on a successful login, so
    the next 429 (if any) starts the escalation from the base cooldown again."""
    client = _redis_client()
    if client is None:
        return
    try:
        client.delete(_COOLDOWN_KEY, _TRIP_COUNT_KEY)
    except Exception:
        pass


# --- Manual global automation pause -------------------------------------------------
# A kill-switch to halt ALL Selenium automation (feed commenting, replies, DMs, stats, invites) for
# a set window so a rate-limited account/IP can recover. Enforced centrally in login_to_linkedin
# (every Selenium task logs in) and short-circuited by the high-volume beat dispatchers. Redis-backed
# with a TTL so it auto-expires; fails open (no Redis → not paused) like the breaker.

def pause_automation(seconds: int, reason: str = "manual") -> bool:
    """Pause all Selenium automation for `seconds`. Returns True if the pause was stored."""
    client = _redis_client()
    if client is None:
        return False
    try:
        client.set(_PAUSE_KEY, reason or "manual", ex=max(1, int(seconds)))
        log_warning(f"Automation PAUSED for {int(seconds)}s (reason: {reason})", action_type="rate_limit")
        return True
    except Exception as e:
        log_warning("Failed to set automation pause", exc=e, action_type="rate_limit")
        return False


def resume_automation() -> bool:
    """Lift a manual pause immediately. Returns True on success (or no-op when nothing was paused)."""
    client = _redis_client()
    if client is None:
        return False
    try:
        client.delete(_PAUSE_KEY)
        return True
    except Exception:
        return False


def automation_pause_remaining() -> int:
    """Seconds left on a manual pause, or 0 if not paused / Redis unavailable."""
    client = _redis_client()
    if client is None:
        return 0
    try:
        ttl = client.ttl(_PAUSE_KEY)
    except Exception:
        return 0
    return ttl if ttl and ttl > 0 else 0


def automation_pause_reason() -> "str | None":
    """The reason string stored with the current pause, or None when nothing is paused.

    Lets a caller tell ITS OWN pause apart from someone else's — the deploy maintenance mode
    (utilities/maintenance.py) only lifts a pause it set, so a 429/manual pause survives a deploy.
    """
    client = _redis_client()
    if client is None:
        return None
    try:
        value = client.get(_PAUSE_KEY)
    except Exception:
        return None
    if value is None:
        return None
    return value.decode("utf-8", "ignore") if isinstance(value, bytes) else str(value)


def is_automation_paused() -> bool:
    return automation_pause_remaining() > 0


# --- per-user commenting quality hold -------------------------------------------------
# Narrower than the global pause above: this stops ONE user's feed commenting when their comments
# are measurably being demoted out of LinkedIn's 'Most relevant' view (issue #628), while leaving
# their posting, replies and DMs alone — the problem is the comments, not the account. Redis-backed
# with a TTL and fails OPEN (no Redis -> not held), like everything else in this module.

_COMMENT_HOLD_KEY = "linkedin:comment_quality_hold:{user_id}"


def hold_commenting(user_id: int, seconds: int, reason: str = "comment quality") -> bool:
    """Hold this user's feed commenting for `seconds`. Returns True if the hold was stored."""
    client = _redis_client()
    if client is None:
        return False
    try:
        client.set(_COMMENT_HOLD_KEY.format(user_id=int(user_id)), reason or "comment quality",
                   ex=max(1, int(seconds)))
        log_warning(f"Feed commenting HELD for user {user_id} for {int(seconds)}s "
                    f"(reason: {reason})", action_type="comment", user_id=int(user_id))
        return True
    except Exception as e:
        log_warning("Failed to set commenting hold", exc=e, action_type="comment")
        return False


def release_commenting_hold(user_id: int) -> bool:
    """Lift a commenting hold immediately (owner action once the comments are fixed)."""
    client = _redis_client()
    if client is None:
        return False
    try:
        client.delete(_COMMENT_HOLD_KEY.format(user_id=int(user_id)))
        return True
    except Exception:
        return False


def commenting_hold_remaining(user_id: int) -> int:
    """Seconds left on this user's commenting hold, or 0 when not held / Redis unavailable."""
    client = _redis_client()
    if client is None:
        return 0
    try:
        ttl = client.ttl(_COMMENT_HOLD_KEY.format(user_id=int(user_id)))
    except Exception:
        return 0
    return ttl if ttl and ttl > 0 else 0


def commenting_hold_reason(user_id: int) -> "str | None":
    """The reason stored with this user's commenting hold, or None when nothing is held."""
    client = _redis_client()
    if client is None:
        return None
    try:
        value = client.get(_COMMENT_HOLD_KEY.format(user_id=int(user_id)))
    except Exception:
        return None
    if value is None:
        return None
    return value.decode("utf-8", "ignore") if isinstance(value, bytes) else str(value)


def is_commenting_held(user_id: int) -> bool:
    return commenting_hold_remaining(user_id) > 0


# --- suppression tripwire state -------------------------------------------------
# The record of WHY engagement was auto-paused for silent-suppression (issue #629). The pause itself
# is the global `pause_automation` breaker above — this is the per-user evidence beside it, so the
# Account banner can explain the stop in plain language and the daily check can tell its own trip
# apart from a manual/maintenance pause. Deliberately has NO TTL: the tripwire never auto-resumes, a
# human clears it via clear_suppression_trip. Fails open (no Redis -> not tripped) like the rest.

_SUPPRESSION_KEY = "linkedin:suppression_trip:{user_id}"
SUPPRESSION_PAUSE_REASON_PREFIX = "suppression"


def suppression_pause_reason(user_id: int) -> str:
    """The `pause_automation` reason string this tripwire writes, tagged with the user whose signals
    tripped it so a later run can tell its own pause apart from a manual or maintenance one."""
    return f"{SUPPRESSION_PAUSE_REASON_PREFIX}:{int(user_id)}"


def is_suppression_pause(reason: "str | None") -> bool:
    return bool(reason) and str(reason).startswith(SUPPRESSION_PAUSE_REASON_PREFIX)


def is_measurement_paused() -> bool:
    """Whether the standing pause also stops READ-ONLY measurement (post-stat / follower capture).

    Every pause does — except the suppression tripwire's own, which must not. The daily stats scrape
    is what produces the very readings the tripwire re-evaluates: freeze it and the engagement trend
    stays stuck at the collapsed numbers forever, so a recovered account can never be seen to have
    recovered and the daily re-arm extends the pause indefinitely. It would also make the notice the
    user is sent ("your scheduled posts still publish and we keep collecting your analytics")
    untrue. The 429 breaker still gates these lanes separately — this only narrows OUR pause.
    """
    if not is_automation_paused():
        return False
    return not is_suppression_pause(automation_pause_reason())


def record_suppression_trip(user_id: int, reason: str, detail: "dict | None" = None,
                            tripped_at: "str | None" = None) -> bool:
    """Persist the trip. Returns True if it was stored."""
    client = _redis_client()
    if client is None:
        return False
    payload = {"user_id": int(user_id), "reason": reason or "suppression",
               "tripped_at": tripped_at or datetime.now(timezone.utc).isoformat(),
               "detail": detail or {}}
    try:
        client.set(_SUPPRESSION_KEY.format(user_id=int(user_id)), json.dumps(payload, default=str))
        return True
    except Exception as e:
        log_warning("Failed to record suppression trip", exc=e, user_id=int(user_id),
                    action_type="rate_limit")
        return False


def clear_suppression_trip(user_id: int) -> bool:
    """Human re-enable: forget the trip. Lifting the automation pause is the caller's separate,
    explicit step — clearing the record must never be what silently restarts engagement."""
    client = _redis_client()
    if client is None:
        return False
    try:
        client.delete(_SUPPRESSION_KEY.format(user_id=int(user_id)))
        return True
    except Exception:
        return False


def suppression_trip_state(user_id: int) -> "dict | None":
    """The stored trip for this user, or None when the tripwire has not fired (or Redis is down)."""
    client = _redis_client()
    if client is None:
        return None
    try:
        raw = client.get(_SUPPRESSION_KEY.format(user_id=int(user_id)))
    except Exception:
        return None
    if raw is None:
        return None
    text = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
    try:
        state = json.loads(text)
    except ValueError:
        return {"user_id": int(user_id), "reason": text, "tripped_at": None, "detail": {}}
    return state if isinstance(state, dict) else None


def is_suppression_tripped(user_id: int) -> bool:
    return suppression_trip_state(user_id) is not None


# --- single-flight task locks -------------------------------------------------
# A per-user run lock so overlapping schedules of the SAME Selenium task (e.g. feed commenting
# fired by the pre-post trigger, the golden-hour beat, and its own self-requeue) can't run
# concurrently and double-act on the feed. Fails OPEN: if Redis is unavailable the lock no-ops
# (returns a sentinel token) so behaviour is unchanged rather than blocking the task entirely.
_LOCK_PREFIX = "linkedin:runlock:"
_LOCK_FAILOPEN = ("no-redis", "lock-error")


def acquire_run_lock(name: str, ttl_seconds: int = 1800) -> "str | None":
    """Try to take a named single-flight lock. Returns an opaque token to pass to
    release_run_lock() if acquired (or a fail-open sentinel if Redis is down), else None when
    another holder is active. TTL auto-expires the lock if the holder crashes without releasing."""
    client = _redis_client()
    if client is None:
        return "no-redis"  # fail open — don't block the task when Redis is unavailable
    token = f"{os.getpid()}-{name}"
    try:
        if client.set(f"{_LOCK_PREFIX}{name}", token, nx=True, ex=max(1, int(ttl_seconds))):
            return token
        return None
    except Exception as e:
        log_warning("Run-lock acquire failed — proceeding without lock", exc=e, action_type="rate_limit")
        return "lock-error"  # fail open


def release_run_lock(name: str, token: "str | None") -> None:
    """Release a lock only if we still hold the same token (never free another holder's lock, e.g.
    one that TTL-expired and was reacquired). No-ops for the fail-open sentinels."""
    if not token or token in _LOCK_FAILOPEN:
        return
    client = _redis_client()
    if client is None:
        return
    try:
        current = client.get(f"{_LOCK_PREFIX}{name}")
        if current is not None and current.decode("utf-8", "ignore") == token:
            client.delete(f"{_LOCK_PREFIX}{name}")
    except Exception:
        pass
