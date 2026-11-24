"""The MCP kill switch: the operator's stop for `POST /api/mcp`, checked before any auth step.

Two switches, either one stops the endpoint: the `MCP_SERVER_KILL` env var (ships only through a
deploy) and the `mcp:kill` Redis key (takes effect on the next request). It is a SAFETY control,
so it is never a feature flag, and it fails CLOSED: a Redis it cannot read means the endpoint is
unavailable, not that the switch is off. `docs/agent-mcp-plan.md` §3.3 step 1.
"""

import os
from enum import Enum
from typing import Any, Optional

from cqc_lem.utilities.logger import log_debug

KILL_ENV_VAR = "MCP_SERVER_KILL"
KILL_REDIS_KEY = "mcp:kill"

# Values that read as "off" for the env switch. Anything else that is non-empty kills the endpoint,
# because a typo in a stop switch must stop, not run.
_OFF_VALUES = ("0", "false", "no", "off")


class KillState(Enum):
    """What the kill switch reads. `UNREADABLE` is its own state so the caller fails closed on it."""

    OFF = "off"
    ON = "on"
    UNREADABLE = "unreadable"


def kill_env_set() -> bool:
    """Return True when `MCP_SERVER_KILL` is set to anything that does not read as off.

    Read at call time, never at import, so a test (or a restarted worker) sees the current value.
    """
    raw = (os.getenv(KILL_ENV_VAR) or "").strip().lower()
    return bool(raw) and raw not in _OFF_VALUES


# The kill read has its OWN client, never `shared_redis_client()`: that one waits 2s on a socket
# and redis-py retries by default, and every MCP request that passes the header checks waits on
# this read. 0.5s with no retry bounds a dead Redis to about half a second per request, measured at
# 0.50s against an unroutable host where the shared client took 2.01s. A timeout is UNREADABLE,
# which already fails closed, so retrying would only make a refusal slower.
KILL_READ_TIMEOUT_SECONDS = 0.5

# One client per (pid, url), the same rule `rate_limit._CLIENT_STATE` documents: `from_url` builds
# its own pool, and a pool made before a fork must not be shared with the child.
_CLIENT_STATE: dict[str, Any] = {"client": None, "pid": None, "url": None}


def reset_kill_client() -> None:
    """Drop the cached kill-switch client so the next read rebuilds it (tests, a changed URL)."""
    _CLIENT_STATE.update(client=None, pid=None, url=None)


def _redis() -> Optional[Any]:
    """This process's kill-switch Redis client, or None when one cannot be built.

    Typed `Any`: `redis` is imported lazily, as `rate_limit` does, so this module does not load it
    at import time. A failure to build is never cached; the next request tries again.
    """
    try:
        import redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry
    except Exception:  # noqa: BLE001 — no redis package means no kill read: fail closed.
        return None
    from cqc_lem.utilities.linkedin.rate_limit import redis_url

    url = redis_url()
    pid = os.getpid()
    if (_CLIENT_STATE["client"] is not None and _CLIENT_STATE["pid"] == pid
            and _CLIENT_STATE["url"] == url):
        return _CLIENT_STATE["client"]
    try:
        client = redis.Redis.from_url(url, socket_timeout=KILL_READ_TIMEOUT_SECONDS,
                                      socket_connect_timeout=KILL_READ_TIMEOUT_SECONDS,
                                      retry=Retry(NoBackoff(), 0))
    except Exception:  # noqa: BLE001 — same answer as an unreadable key: fail closed.
        return None
    _CLIENT_STATE.update(client=client, pid=pid, url=url)
    return client


def read_kill_key() -> KillState:
    """Read the `mcp:kill` Redis key. Blocking: call it through `run_in_threadpool`.

    Any stored value counts as ON. The admin toggle (phase P2) deletes the key to turn it off, so a
    present key is never ambiguous, and treating every value as ON keeps a malformed write on the
    stopped side.

    Returns:
        `KillState.ON` when the key exists, `OFF` when it does not, and `UNREADABLE` when there is
        no Redis handle or the read raised.
    """
    client = _redis()
    if client is None:
        log_debug("MCP kill key unreadable: no Redis handle", key=KILL_REDIS_KEY)
        return KillState.UNREADABLE
    try:
        value = client.get(KILL_REDIS_KEY)
    except Exception as exc:  # noqa: BLE001 — any Redis fault is the same answer: fail closed.
        log_debug("MCP kill key unreadable: Redis read failed", key=KILL_REDIS_KEY,
                  error_type=type(exc).__name__)
        return KillState.UNREADABLE
    return KillState.OFF if value is None else KillState.ON
