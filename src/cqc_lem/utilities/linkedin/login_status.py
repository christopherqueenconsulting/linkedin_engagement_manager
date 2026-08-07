"""Per-user LinkedIn sign-in status, for the Account page (issue #933).

When LinkedIn challenges an automated sign-in it asks the account owner to confirm the device
from the LinkedIn mobile app, and LEM emails them to go and tap "Yes". The approval itself
happens entirely on LinkedIn's side, so a user who had already approved could not tell whether
LEM ever saw it: the app said "a session is saved" before the approval and exactly the same
thing after. This records the outcome of the sign-in the approval belongs to, so the SPA can
answer "did my approval land?" instead of leaving the user guessing.

State lives in Redis next to the 429 breaker (and reuses its handle): it is short-lived runtime
state, it survives a deploy, and it needs no migration. Fails open — with Redis unavailable
every write no-ops and `get_login_status` returns None, so a sign-in never breaks because
status reporting is down.
"""

import json
import os
from datetime import datetime, timezone
from enum import StrEnum
from typing import Optional

from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client
from cqc_lem.utilities.logger import log_debug

_KEY_PREFIX = "linkedin:login_status:"
_DEFAULT_TTL_SECONDS = 30 * 24 * 60 * 60   # a "last signed in" fact stays useful for weeks
_DEFAULT_PENDING_TTL_SECONDS = 15 * 60     # a stalled run must not say "waiting for you" forever


class LinkedInLoginState(StrEnum):
    """What the last LinkedIn sign-in attempt did about the device-approval challenge."""

    SIGNED_IN = 'signed_in'                    # signed in — any approval asked for landed
    APPROVAL_PENDING = 'approval_pending'      # LinkedIn asked; we emailed and are waiting
    APPROVAL_TIMED_OUT = 'approval_timed_out'  # we stopped waiting; the next run asks again


def _ttl_seconds() -> int:
    try:
        return int(os.getenv("LINKEDIN_LOGIN_STATUS_TTL_SECONDS", str(_DEFAULT_TTL_SECONDS)))
    except ValueError:
        return _DEFAULT_TTL_SECONDS


def _pending_ttl_seconds() -> int:
    """A PENDING record expires on its own so a worker that died mid-challenge cannot leave the
    Account page telling the user to approve a sign-in nobody is waiting on any more.
    """
    try:
        return int(os.getenv("LINKEDIN_LOGIN_STATUS_PENDING_TTL_SECONDS",
                             str(_DEFAULT_PENDING_TTL_SECONDS)))
    except ValueError:
        return _DEFAULT_PENDING_TTL_SECONDS


def _key(user_id: int) -> str:
    return f"{_KEY_PREFIX}{int(user_id)}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_recent(iso: Optional[str], seconds: int) -> bool:
    """Whether a timestamp we wrote is younger than `seconds`. A record that can't be parsed is
    treated as old, so a stale approval is never re-claimed as this sign-in's.
    """
    if not iso:
        return False
    try:
        ts = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() <= seconds


def _read(user_id: int) -> Optional[dict]:
    client = shared_redis_client()
    if client is None:
        return None
    try:
        raw = client.get(_key(user_id))
    except Exception:
        return None
    if not raw:
        return None
    try:
        status = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return status if isinstance(status, dict) else None


def _write(user_id: int, status: dict, ttl: int) -> None:
    client = shared_redis_client()
    if client is None:
        return
    status["updated_at"] = _now()
    try:
        client.set(_key(user_id), json.dumps(status), ex=ttl)
    except Exception as e:
        # Status reporting is never worth failing a sign-in over, and a Redis blip is an expected
        # no-op rather than a defect — DEBUG, not a warning that would file one on repeat.
        log_debug(f"Could not persist LinkedIn login status: {e}", user_id=user_id,
                  action_type="login")


def mark_approval_pending(user_id: int) -> None:
    """LinkedIn raised the device-approval challenge and the user has been emailed about it."""
    existing = _read(user_id) or {}
    _write(user_id, {
        "state": str(LinkedInLoginState.APPROVAL_PENDING),
        "approval_requested_at": _now(),
        # Keep the last good sign-in: "you approved on the 2nd, we're asking again now" is a very
        # different message from "we have never signed in".
        "signed_in_at": existing.get("signed_in_at"),
    }, ttl=_pending_ttl_seconds())


def mark_approval_timed_out(user_id: int) -> None:
    """The approval window closed without the sign-in clearing."""
    existing = _read(user_id) or {}
    _write(user_id, {
        "state": str(LinkedInLoginState.APPROVAL_TIMED_OUT),
        "approval_requested_at": existing.get("approval_requested_at"),
        "signed_in_at": existing.get("signed_in_at"),
    }, ttl=_ttl_seconds())


def mark_signed_in(user_id: int) -> None:
    """A sign-in completed. Recorded at the cookie persist, where both of `login_to_linkedin`'s
    success paths meet, and again the moment a device approval clears — a login that dies between
    the two must not leave the SPA telling a user who already tapped Yes to go and tap it.

    `approval_cleared_at` is set only when this sign-in followed a pending approval — that is the
    exact fact the reporter could not see: the tap they already made was received.
    """
    existing = _read(user_id) or {}
    was_pending = existing.get("state") == str(LinkedInLoginState.APPROVAL_PENDING)
    # The cookie persist writes again for the SAME sign-in the approval just cleared, so carry the
    # approval across rather than erasing it. Bounded by the pending window — no single login
    # attempt outlives it — so a routine sign-in weeks later never re-claims an old approval.
    same_attempt = (existing.get("state") == str(LinkedInLoginState.SIGNED_IN)
                    and _is_recent(existing.get("approval_cleared_at"), _pending_ttl_seconds()))
    now = _now()
    _write(user_id, {
        "state": str(LinkedInLoginState.SIGNED_IN),
        "signed_in_at": now,
        "approval_requested_at": (existing.get("approval_requested_at")
                                  if was_pending or same_attempt else None),
        "approval_cleared_at": (now if was_pending
                                else existing.get("approval_cleared_at") if same_attempt
                                else None),
    }, ttl=_ttl_seconds())


def get_login_status(user_id: int) -> Optional[dict]:
    """The last recorded sign-in state for the user, or None when nothing is recorded (no run has
    signed in since the record expired, or Redis is unavailable).
    """
    return _read(user_id)
