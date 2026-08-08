"""On-demand LinkedIn profile re-scrape — the ONE place a manual refresh is claimed (issue #1076).

A user who reorders their skills or rewrites their headline wants LEM writing from the NEW profile
today, not whenever the weekly staleness beat (`run_scheduler.auto_refresh_profile_syntheses`)
catches up. The button that makes that immediate spends a Chrome session slot out of the fixed pool
every Selenium lane shares, so it is bounded here rather than at the endpoint: one claim per user
per fixed 24h window, counted in Redis.

Two properties are load-bearing:

- **It FAILS OPEN.** Redis is the broker; when it is unavailable the API must not stop answering a
  button press. An unclaimed refresh costs one browser session, a refusal costs the feature — same
  posture as `auth_rate_limit` and `human_pacing`.
- **A spent window is an EXPECTED no-op, not a failure.** The second press of the day is a person
  pressing a button twice, so it logs DEBUG and the endpoint still answers 202 saying it did not
  queue. Warning here would file a defect for working behaviour (`utilities/CLAUDE.md`).

The window is FIXED, not sliding: the TTL is set only on the first increment, so a burst of presses
cannot push the reset further out than 24h from the first one.
"""

from dataclasses import dataclass

from cqc_lem.utilities.env_constants import PROFILE_REFRESH_MAX_PER_DAY
from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client
from cqc_lem.utilities.logger import log_debug, log_warning

WINDOW_SECONDS = 24 * 60 * 60
_KEY_PREFIX = "lem:profile_refresh"

# Reasons, so the SPA renders one of three sentences instead of parsing prose.
REASON_QUEUED = "queued"
REASON_ALREADY_REFRESHED_TODAY = "already_refreshed_today"


@dataclass(frozen=True)
class RefreshClaim:
    """`queued` is what the endpoint acts on; `reason` is what the SPA renders.

    `retry_after_seconds` is what remains of the window — 0 when the claim was granted, so a caller
    never has to special-case "granted" to know there is nothing to wait for.
    """

    queued: bool
    reason: str
    retry_after_seconds: int = 0


def _key(user_id: int) -> str:
    return f"{_KEY_PREFIX}:{user_id}"


def _remaining_seconds(client, user_id: int) -> int:
    """What is left of this user's window, or the full window when Redis cannot say.

    A key with no TTL (`-1`) or none at all (`-2`) reads as the full window rather than 0: telling
    the SPA to re-enable the button immediately is the one answer that is certainly wrong.
    """
    try:
        ttl = int(client.ttl(_key(user_id)))
    except Exception:
        return WINDOW_SECONDS
    return ttl if ttl > 0 else WINDOW_SECONDS


def claim_profile_refresh(user_id: int) -> RefreshClaim:
    """Claim this user's one refresh for the window, or report that it is already spent.

    Call this BEFORE dispatching the task: the claim is what makes a double-click cost one browser
    session rather than two.
    """
    client = shared_redis_client()
    if client is None:
        # Fail open — see the module docstring. DEBUG because a broker restart is not this
        # function's defect to report; `shared_redis_client` already says so where it happens.
        log_debug("Profile-refresh limiter unavailable — allowing the refresh", user_id=user_id)
        return RefreshClaim(queued=True, reason=REASON_QUEUED)
    try:
        count = int(client.incr(_key(user_id)))
        if count == 1:
            client.expire(_key(user_id), WINDOW_SECONDS)
    except Exception as e:
        log_warning("Profile-refresh limiter failed — allowing the refresh", exc=e, user_id=user_id)
        return RefreshClaim(queued=True, reason=REASON_QUEUED)
    if count <= PROFILE_REFRESH_MAX_PER_DAY:
        return RefreshClaim(queued=True, reason=REASON_QUEUED)
    log_debug("Profile refresh already claimed for this window", user_id=user_id)
    return RefreshClaim(queued=False, reason=REASON_ALREADY_REFRESHED_TODAY,
                        retry_after_seconds=_remaining_seconds(client, user_id))


def refresh_claimed_seconds(user_id: int) -> int:
    """Seconds until this user may refresh again, or 0 when they may refresh now.

    A read-only PEEK — it never increments, so the SPA can render the disabled state on every page
    load without spending the window it is reporting on. Fails open to 0 for the same reason the
    claim fails open: an unreadable limiter must leave the button usable, and the claim is the thing
    that actually enforces the bound.
    """
    client = shared_redis_client()
    if client is None:
        return 0
    try:
        raw = client.get(_key(user_id))
        count = int(raw) if raw is not None else 0
    except Exception as e:
        log_warning("Could not read the profile-refresh window", exc=e, user_id=user_id)
        return 0
    if count < PROFILE_REFRESH_MAX_PER_DAY:
        return 0
    return _remaining_seconds(client, user_id)
