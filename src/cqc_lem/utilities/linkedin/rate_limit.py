"""Shared circuit breaker for LinkedIn HTTP 429 rate-limiting.

LinkedIn rate-limits by egress IP, so a 429 hit by one engagement task means every
other Selenium task (comments, replies, viewer DMs, appreciation DMs) will also be
throttled. Without coordination each task independently spins up a browser, navigates
to the feed, and re-trips the limit — which prolongs the block. This breaker records
the 429 in Redis with a cooldown TTL so subsequent tasks skip the LinkedIn navigation
until it expires. Fails open: if Redis is unavailable the breaker no-ops and callers
behave as before.
"""

import os

from cqc_lem.utilities.logger import log_warning

_COOLDOWN_KEY = "linkedin:429_cooldown"
_TRIP_COUNT_KEY = "linkedin:429_trip_count"   # consecutive trips → escalating cooldown
_PAUSE_KEY = "linkedin:automation_paused"     # manual global Selenium pause
_DEFAULT_COOLDOWN_SECONDS = 1800  # 30 min
_DEFAULT_MAX_COOLDOWN_SECONDS = 6 * 60 * 60  # cap the escalation at 6h
_TRIP_COUNT_TTL = 24 * 60 * 60  # remember consecutive trips for a day


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


def mark_rate_limited(reason: str = "") -> None:
    client = _redis_client()
    if client is None:
        return
    try:
        # Escalating back-off: each CONSECUTIVE trip (counter cleared only by a successful login)
        # doubles the cooldown — base, 2x, 4x, … up to a cap. A fixed 30-min cooldown meant that as
        # soon as it expired some task probed LinkedIn, drew a fresh 429 and re-tripped it every
        # ~30 min forever (the doom loop). Escalation probes less and less often so the throttled IP
        # can actually recover.
        try:
            trips = int(client.incr(_TRIP_COUNT_KEY))
            client.expire(_TRIP_COUNT_KEY, _TRIP_COUNT_TTL)
        except Exception:
            trips = 1
        seconds = min(_max_cooldown_seconds(), _cooldown_seconds() * (2 ** max(0, trips - 1)))
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


def is_automation_paused() -> bool:
    return automation_pause_remaining() > 0
