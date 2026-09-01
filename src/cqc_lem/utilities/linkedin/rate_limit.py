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

from cqc_lem.utilities.logger import log_info, log_warning

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


def _resolve_redis_url() -> str:
    """The Redis URL this process should use.

    The Celery broker URL when it points at Redis; on AWS the broker is SQS and the result backend
    is Redis, so fall back to that, then to the local default.
    """
    url = os.getenv("CELERY_BROKER_URL", "")
    if not url.startswith("redis"):
        url = os.getenv("CELERY_RESULT_BACKEND", "")
    if not url.startswith("redis"):
        url = f"redis://redis:{os.getenv('REDIS_PORT', '6379')}/0"
    return url


# One client per (pid, url). `Redis.from_url` builds its OWN ConnectionPool every call, and the
# pool disconnects when the object is collected — so re-deriving the handle per operation cost a
# TCP handshake per Redis command, on paths that run per Selenium action. Keyed on pid because
# Celery forks its workers: a pool built before the fork would hand the same socket to parent and
# child, the same hazard `db.py`'s pool is pid-keyed for.
_CLIENT_STATE: dict = {"client": None, "pid": None, "url": None}


def reset_redis_client() -> None:
    """Drop the cached handle so the next call rebuilds it (tests, and a changed URL)."""
    _CLIENT_STATE.update(client=None, pid=None, url=None)


def _redis_client():
    """Redis handle for the breaker, or None if unavailable (breaker then no-ops).

    Cached per process — see `_CLIENT_STATE`. Never caches a failure: an unavailable Redis returns
    None and is retried on the next call, because "Redis was down once" must not become "the
    breaker is off for the life of this worker".
    """
    try:
        import redis
    except Exception:
        return None
    url = _resolve_redis_url()
    pid = os.getpid()
    if (_CLIENT_STATE["client"] is not None
            and _CLIENT_STATE["pid"] == pid
            and _CLIENT_STATE["url"] == url):
        return _CLIENT_STATE["client"]
    try:
        client = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
    except Exception:
        return None
    _CLIENT_STATE.update(client=client, pid=pid, url=url)
    return client


def shared_redis_client():
    """Public handle to the same Redis this breaker uses (None when unavailable), so other
    runtime-state helpers reuse one URL-resolution rule instead of re-deriving it.
    """
    return _redis_client()


def mark_rate_limited(reason: str = "") -> None:
    """Trip the breaker — one task's 429 stops the Selenium lanes for everyone on this egress IP.

    The cooldown DOUBLES per consecutive trip (up to the cap) rather than staying fixed, because a
    fixed window is what produced the doom loop: it expired, some task probed LinkedIn, drew a fresh
    429 and re-tripped it every 30 minutes for days. `reason` is stored as the breaker's value purely
    for whoever is reading Redis later.

    Fails open and silently: with Redis unavailable this is a no-op, so a caller may never treat a
    return from here as proof the breaker is open.
    """
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
        # The warning above stops at the log; PostHog only forwards ERROR and up by default. Emit
        # the trip as its own event so the 429 dashboard tile and its spike alert have a signal
        # (issue #650). Imported here — observability reaches back into this module for Redis.
        # In its own try: the breaker is already OPEN by this point, so a telemetry failure must not
        # fall into the handler below and log "Failed to set the circuit breaker" — that would send
        # whoever is debugging a doom loop looking for a breaker that is in fact working.
        try:
            from cqc_lem.utilities.observability import track_rate_limit_trip
            track_rate_limit_trip(seconds, trips, reason or "429")
        except Exception as e:
            log_warning("Failed to track LinkedIn 429 trip (breaker is open regardless)", exc=e,
                        action_type="rate_limit")
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
    the next 429 (if any) starts the escalation from the base cooldown again.
    """
    client = _redis_client()
    if client is None:
        return
    try:
        client.delete(_COOLDOWN_KEY, _TRIP_COUNT_KEY)
    except Exception as e:
        log_warning("Could not clear rate-limit keys", exc=e)


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
    stored_reason = reason or "manual"
    try:
        client.set(_PAUSE_KEY, stored_reason, ex=max(1, int(seconds)))
        # INFO, not WARNING (issue #917): storing a pause is a deliberate state transition, never a
        # degraded path detected HERE. Every caller that considers its own pause a defect already
        # says so where it detects it — the suppression tripwire escalates CRITICAL (#629), the 429
        # breaker warns in mark_rate_limited, and maintenance mode logs its own INFO with the drain
        # detail. The deploy pause is routine (one per release, 4x daily), so a warning here was
        # re-emitted at ERROR on repeat and filed a grouped $exception for working behaviour.
        log_info(f"Automation PAUSED for {int(seconds)}s (reason: {stored_reason})",
                 action_type="rate_limit")
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
    """Whether the manual/deploy/suppression kill-switch is standing right now.

    Only that switch — the 429 breaker is a separate, independent gate
    (`rate_limit_cooldown_remaining`), so neither answer implies the other. Fails open: no Redis
    reads as NOT paused, because a Redis outage must not be able to freeze every Selenium lane.
    """
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
    """Whether THIS user's feed commenting is held on comment quality (issue #628).

    Narrower than the global pause on purpose: their posting, replies and DMs keep running, because
    the measured problem is the comments and not the account. Fails open (no Redis -> not held).
    """
    return commenting_hold_remaining(user_id) > 0


# The same shape again, for connection invites (#1733/#1732). Two things trip it, and neither is a
# reason to stop the rest of the account:
#
#   * LinkedIn naming an account-level invitation limit or restriction — a WEEKLY ceiling, hence the
#     default TTL. `pause_automation` would be wrong: it is the global Selenium pause, and it would
#     stop commenting, DMs, the feed walk and the newsletter for a week over an invite quota. A 429
#     trip would be wrong too: the breaker is about HTTP throttling and escalates a SHARED cooldown.
#   * a run of invites that could not open a Connect dialog at all. Those failures cost nothing
#     against `max_invites_per_day` (which counts successful sends off immutable `logs` rows), so a
#     dead selector turned a queue backlog into ~20 automated profile visits in twelve hours —
#     precisely the surface LinkedIn's automation detection watches. This is a SAFETY control, so it
#     is deliberately not a feature flag.
#
# Fails OPEN, like everything else here: with Redis down the worst case is one wasted Chrome session
# that re-detects the wall and re-sets the hold, which self-heals. Failing closed would freeze a
# healthy account's outbound on an infrastructure blip.
_INVITE_HOLD_KEY = "linkedin:invite_hold:{user_id}"
_INVITE_FAILURE_KEY = "linkedin:invite_dialog_misses:{user_id}"

INVITE_HOLD_DEFAULT_SECONDS = 7 * 24 * 3600  # the ceiling LinkedIn enforces is weekly
INVITE_MISS_HOLD_SECONDS = 6 * 3600          # a dead route: stop for the day, not the week
INVITE_MISS_STREAK_LIMIT = 3


def hold_invites(user_id: int, seconds: int = INVITE_HOLD_DEFAULT_SECONDS,
                 reason: str = "invite limit") -> bool:
    """Hold this user's outbound connection invites for `seconds`. True if the hold was stored."""
    client = _redis_client()
    if client is None:
        return False
    try:
        client.set(_INVITE_HOLD_KEY.format(user_id=int(user_id)), reason or "invite limit",
                   ex=max(1, int(seconds)))
        # INFO, not WARNING: storing a hold is a state transition, not a degraded path detected
        # HERE — the dead Connect route already warns where _open_connect_invite_dialog finds it,
        # so warning again filed a SECOND grouped issue for one breakage. Precedent:
        # pause_automation (#917). Escalation mechanics: docs/error-tracking.md.
        log_info(f"Connection invites HELD for user {user_id} for {int(seconds)}s "
                 f"(reason: {reason})", action_type="invite_connect", user_id=int(user_id))
        return True
    except Exception as e:
        # user_id is passed RAW, not int(): this branch also catches an int() that raised, and
        # re-coercing here would turn a fail-open control into a raise. _extra() coerces it.
        log_warning("Failed to set invite hold", exc=e, action_type="invite_connect",
                    user_id=user_id)
        return False


def release_invite_hold(user_id: int) -> bool:
    """Lift an invite hold immediately (owner action once the wall has cleared)."""
    client = _redis_client()
    if client is None:
        return False
    try:
        client.delete(_INVITE_HOLD_KEY.format(user_id=int(user_id)))
        client.delete(_INVITE_FAILURE_KEY.format(user_id=int(user_id)))
        return True
    except Exception:
        return False


def invite_hold_remaining(user_id: int) -> int:
    """Seconds left on this user's invite hold, or 0 when not held / Redis unavailable."""
    client = _redis_client()
    if client is None:
        return 0
    try:
        ttl = client.ttl(_INVITE_HOLD_KEY.format(user_id=int(user_id)))
    except Exception:
        return 0
    return ttl if ttl and ttl > 0 else 0


def invite_hold_reason(user_id: int) -> "str | None":
    """The reason stored with this user's invite hold, or None when nothing is held."""
    client = _redis_client()
    if client is None:
        return None
    try:
        value = client.get(_INVITE_HOLD_KEY.format(user_id=int(user_id)))
    except Exception:
        return None
    if value is None:
        return None
    return value.decode("utf-8", "ignore") if isinstance(value, bytes) else str(value)


def is_invites_held(user_id: int) -> bool:
    """Whether THIS user's connection invites are held. Fails open (no Redis -> not held)."""
    return invite_hold_remaining(user_id) > 0


def record_invite_dialog_miss(user_id: int) -> int:
    """Count one invite that could not open a Connect dialog, and hold the lane at the limit.

    The streak key expires on its own, so a single miss between working invites can never accumulate
    into a hold across days — only a genuine RUN of them does. Returns the streak length; 0 when
    Redis is unavailable, which is the fail-open answer (nothing counted, nothing held).
    """
    client = _redis_client()
    if client is None:
        return 0
    key = _INVITE_FAILURE_KEY.format(user_id=int(user_id))
    try:
        streak = int(client.incr(key))
        if streak == 1:
            client.expire(key, INVITE_MISS_HOLD_SECONDS)
    except Exception as e:
        log_warning("Failed to count an invite dialog miss", exc=e, action_type="invite_connect")
        return 0
    if streak >= INVITE_MISS_STREAK_LIMIT and not is_invites_held(user_id):
        hold_invites(user_id, INVITE_MISS_HOLD_SECONDS,
                     reason=f"{streak} consecutive invites could not open a Connect dialog")
    return streak


def clear_invite_dialog_misses(user_id: int) -> None:
    """Reset the miss streak — an invite that went out proves the route works."""
    client = _redis_client()
    if client is None:
        return
    try:
        client.delete(_INVITE_FAILURE_KEY.format(user_id=int(user_id)))
    except Exception:
        return


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
    tripped it so a later run can tell its own pause apart from a manual or maintenance one.
    """
    return f"{SUPPRESSION_PAUSE_REASON_PREFIX}:{int(user_id)}"


def is_suppression_pause(reason: "str | None") -> bool:
    """Was this pause set by the suppression tripwire, rather than by a human or a deploy?

    A PREFIX match, so it holds for the per-user form `suppression:<id>` that
    `suppression_pause_reason` writes. Missing or empty reads False: an unattributed pause is treated
    as somebody else's, which is the safe direction — the tripwire only ever lifts its OWN pause.
    """
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
    explicit step — clearing the record must never be what silently restarts engagement.
    """
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
    """Whether the tripwire has fired for this user and no human has cleared it.

    Separate question from whether automation is currently paused: the trip record carries NO TTL, so
    it outlives the pause it caused and only `clear_suppression_trip` ends it. Fails open (no Redis ->
    not tripped), so an outage can never manufacture a standing trip.
    """
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
    another holder is active. TTL auto-expires the lock if the holder crashes without releasing.
    """
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
    one that TTL-expired and was reacquired). No-ops for the fail-open sentinels.
    """
    if not token or token in _LOCK_FAILOPEN:
        return
    client = _redis_client()
    if client is None:
        return
    try:
        current = client.get(f"{_LOCK_PREFIX}{name}")
        if current is not None and current.decode("utf-8", "ignore") == token:
            client.delete(f"{_LOCK_PREFIX}{name}")
    except Exception as e:
        log_warning("Could not release run lock", exc=e, lock_name=name)
