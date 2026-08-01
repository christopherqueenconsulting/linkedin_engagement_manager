"""Rate limiting for the email-PIN auth endpoints (issue #745, phase 2b).

Before this, `/api/auth/email/init` and `/api/auth/email/verify` were unlimited against a 6-digit
PIN space — 10^6 guesses with nothing in the way, and a free mail bomb aimed at any address.

Two layers, deliberately different in kind:

- **This module** — fixed-window counters in Redis, per identity AND per client IP. It bounds how
  many PINs an address can be sent and how many verify calls one IP can make. It **fails open**
  when Redis is unavailable (same posture as `human_pacing`): the API must not stop authenticating
  users because the broker restarted.
- **`db.verify_pin_for_email`** — the DB-backed attempt counter that locks an outstanding PIN after
  `PIN_MAX_ATTEMPTS`. That one is durable and is the hard gate on guessing a live PIN.

The IP is hashed (`crypto.hash_client_ip`) before it becomes a Redis key, so the broker never holds
a plaintext address book of who tried to sign in.
"""

from dataclasses import dataclass
from typing import Optional

from cqc_lem.utilities.crypto import hash_client_ip
from cqc_lem.utilities.env_constants import (
    AUTH_INIT_MAX_PER_HOUR,
    AUTH_IP_MAX_PER_HOUR,
    AUTH_VERIFY_MAX_PER_HOUR,
)
from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client
from cqc_lem.utilities.logger import log_debug, log_warning

_WINDOW_SECONDS = 3600
_KEY_PREFIX = "lem:auth_rl"


@dataclass(frozen=True)
class RateLimitVerdict:
    """`allowed` is what the endpoint acts on; `scope` names which bucket ran out, so the audit row
    can say whether it was the address or the network being throttled."""
    allowed: bool
    scope: Optional[str] = None
    retry_after_seconds: int = _WINDOW_SECONDS


_ALLOWED = RateLimitVerdict(allowed=True)


def _bump(bucket: str, identity: str, limit: int) -> Optional[bool]:
    """Increment one fixed-window counter. True = within the limit, False = over it, None = Redis
    unavailable (fail open). The TTL is set only on the first increment of a window, so the window
    is fixed rather than sliding forward with every request."""
    client = shared_redis_client()
    if client is None:
        return None
    key = f"{_KEY_PREFIX}:{bucket}:{identity}"
    try:
        count = int(client.incr(key))
        if count == 1:
            client.expire(key, _WINDOW_SECONDS)
        return count <= limit
    except Exception as e:
        log_warning("Auth rate limiter unavailable — failing open", exc=e)
        return None


def _retry_after(bucket: str, identity: str) -> int:
    client = shared_redis_client()
    if client is None:
        return _WINDOW_SECONDS
    try:
        ttl = int(client.ttl(f"{_KEY_PREFIX}:{bucket}:{identity}"))
        return ttl if ttl > 0 else _WINDOW_SECONDS
    except Exception:
        return _WINDOW_SECONDS


def _check(bucket: str, identity: Optional[str], limit: int) -> RateLimitVerdict:
    if not identity:
        return _ALLOWED
    within = _bump(bucket, identity, limit)
    if within is None or within:
        return _ALLOWED
    log_debug(f"Auth rate limit hit on {bucket}")
    return RateLimitVerdict(allowed=False, scope=bucket,
                            retry_after_seconds=_retry_after(bucket, identity))


def check_auth_init(email: str, ip: Optional[str] = None) -> RateLimitVerdict:
    """Bound PIN emails per address and per client IP. Counted even when the address is unknown —
    probing for which addresses exist is exactly what this stops."""
    verdict = _check("init_email", (email or "").strip().lower(), AUTH_INIT_MAX_PER_HOUR)
    if not verdict.allowed:
        return verdict
    return _check("init_ip", hash_client_ip(ip), AUTH_IP_MAX_PER_HOUR)


def check_auth_verify(email: str, ip: Optional[str] = None) -> RateLimitVerdict:
    """Bound PIN submissions. The per-IP bucket is what stops one host walking the PIN space across
    many addresses; the per-email bucket stops it walking one address from many hosts."""
    verdict = _check("verify_email", (email or "").strip().lower(), AUTH_VERIFY_MAX_PER_HOUR)
    if not verdict.allowed:
        return verdict
    return _check("verify_ip", hash_client_ip(ip), AUTH_IP_MAX_PER_HOUR)


def clear_auth_limits(email: str, ip: Optional[str] = None) -> None:
    """Drop this identity's counters — called after a SUCCESSFUL login so a user who fat-fingered
    their PIN four times isn't throttled for the rest of the hour."""
    client = shared_redis_client()
    if client is None:
        return
    identities = [("init_email", (email or "").strip().lower()),
                  ("verify_email", (email or "").strip().lower())]
    ip_hash = hash_client_ip(ip)
    if ip_hash:
        identities += [("init_ip", ip_hash), ("verify_ip", ip_hash)]
    for bucket, identity in identities:
        if not identity:
            continue
        try:
            client.delete(f"{_KEY_PREFIX}:{bucket}:{identity}")
        except Exception as e:
            log_debug(f"Could not clear auth rate-limit key {bucket}: {e}")
