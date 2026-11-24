"""The MCP kill switch: the operator's stop for `POST /api/mcp`, checked before anything else.

Two switches, either one stops the endpoint: the `MCP_SERVER_KILL` env var (ships only through a
deploy) and the `mcp:kill` Redis key (takes effect on the next request). It is a SAFETY control,
so it is never a feature flag, and it fails CLOSED: a Redis it cannot read means the endpoint is
unavailable, not that the switch is off. `docs/agent-mcp-plan.md` §3.3 step 1.
"""

import os
from enum import Enum

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


def _redis():
    """The process's shared Redis handle, or None when Redis is unavailable."""
    from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client

    return shared_redis_client()


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
