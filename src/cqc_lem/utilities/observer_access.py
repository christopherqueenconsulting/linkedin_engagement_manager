"""Once-a-day access record for an `observer` session (docs/agent-roles.md).

An observer only ever reads, so its requests are not audited one by one — a row per poll would bury
every human sign-in in the account's recent-events list. What the owner needs is a durable answer to
"was this read-only token used, and when?" without scanning access logs. So the first successful
observer request of each UTC day, per session, claims a Redis key here and the caller writes ONE
`observer_access` audit row. A second request the same day finds the key and writes nothing.

Fails OPEN, the way `profile_refresh` does: with Redis unreachable the claim succeeds, so a broker
outage can never turn a read into an error. The cost of failing open is the opposite of the usual
one — during an outage each observer request may write a row instead of one per day — and that was
chosen over losing the access record.
"""

from datetime import datetime, timezone

from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client
from cqc_lem.utilities.logger import log_debug

# 25h, not 24h: the day is in the key, so the TTL only has to outlive the day it names.
WINDOW_SECONDS = 25 * 60 * 60
_KEY_PREFIX = "lem:observer_access"


def _key(session_key: str) -> str:
    return f"{_KEY_PREFIX}:{datetime.now(timezone.utc).strftime('%Y%m%d')}:{session_key}"


def claim_observer_access(session_key: str) -> bool:
    """True when this is the session's first observer request of the UTC day (write the row)."""
    client = shared_redis_client()
    if client is None:
        log_debug("Observer-access limiter unavailable — recording the access")
        return True
    try:
        return bool(client.set(_key(session_key), "1", nx=True, ex=WINDOW_SECONDS))
    except Exception as e:
        # DEBUG, not WARNING: a broker outage is not this function's defect to report, and a warning
        # per observer request would escalate and file a grouped `$exception` for it.
        log_debug("Observer-access limiter failed — recording the access", exc=e)
        return True
