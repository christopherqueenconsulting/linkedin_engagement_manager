"""The MCP kill switch: the operator's stop for `POST /api/mcp`, checked before any auth step.

Two switches, either one stops the endpoint: the `MCP_SERVER_KILL` env var (ships only through a
deploy) and the `mcp:kill` Redis key (takes effect on the next request). It is a SAFETY control,
so it is never a feature flag, and it fails CLOSED: a Redis it cannot read means the endpoint is
unavailable, not that the switch is off. `docs/agent-mcp-plan.md` §3.3 step 1.

The rest of this module is the gate's later limits (§3.3 steps 7, 10, 11), all in Redis except the
per-worker concurrency cap: the pre-auth failure buckets (per address, and per address + token),
in-flight concurrency, fixed-window budgets and the per-session activity counts the Agents tab
shows. Each Redis-backed limit fails CLOSED by raising `LimitsUnavailable` (the gate answers 503),
with two exceptions that fail OPEN because losing them costs a number, never a control: the activity
counts, and the mint-rate limiter. The mint's real cap is separate and does not need Redis: at most
10 live MCP tokens per account, counted and inserted in ONE database transaction that holds the
user's row lock (`create_session_within_cap`), so two concurrent mints cannot both pass it.

Every counter is created by `_incr_with_ttl`: `SET key 0 EX ttl NX`, then `INCR`. The TTL is set
exactly once, when the key is created, and never by a later attempt, so a refused or leaked
attempt cannot keep a counter alive, and no counter can exist without a TTL.
"""

import hashlib
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Optional

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


def set_kill_key(on: bool) -> bool:
    """Turn the Redis kill switch on (store the key) or off (delete it). Blocking.

    Args:
        on: True stores `mcp:kill`; False deletes it.

    Returns:
        True when Redis took the write, False when there is no handle or the write raised.
    """
    client = _redis()
    if client is None:
        return False
    try:
        if on:
            client.set(KILL_REDIS_KEY, "1")
        else:
            client.delete(KILL_REDIS_KEY)
    except Exception as exc:  # noqa: BLE001 — the caller reports the failed toggle.
        log_debug("MCP kill key write failed", key=KILL_REDIS_KEY, error_type=type(exc).__name__)
        return False
    return True


class LimitsUnavailable(Exception):
    """A fail-closed limit could not read or write Redis. The gate answers 503 `mcp_unavailable`."""


# --- environment-tunable limits (plan §3.3 steps 7, 10, 11; owner ruling O10 defaults) ----------
#
# Read at call time. A value that is not a non-negative integer falls back to the default; 0 is
# honoured and means REFUSE ALL, so an operator can close one budget without the kill switch.

ENV_FAILURES_PER_MIN = "MCP_AUTH_FAILURES_PER_MIN"
ENV_ADDRESS_FAILURES_PER_MIN = "MCP_AUTH_FAILURES_PER_ADDRESS_PER_MIN"
ENV_INFLIGHT_PER_USER = "MCP_MAX_INFLIGHT_PER_USER"
ENV_INFLIGHT_PER_WORKER = "MCP_MAX_INFLIGHT_PER_WORKER"
ENV_TOOL_CALLS_PER_MIN = "MCP_RATE_TOOL_CALLS_PER_MIN"
ENV_READS_PER_MIN = "MCP_RATE_READS_PER_MIN"
ENV_READS_PER_DAY = "MCP_RATE_READS_PER_DAY"
ENV_WRITES_PER_HOUR = "MCP_RATE_WRITES_PER_HOUR"
ENV_WRITES_PER_DAY = "MCP_RATE_WRITES_PER_DAY"
ENV_MINTS_PER_HOUR = "MCP_TOKEN_MINTS_PER_HOUR"

_DEFAULTS = {
    ENV_FAILURES_PER_MIN: 30,
    ENV_ADDRESS_FAILURES_PER_MIN: 60,
    ENV_INFLIGHT_PER_USER: 2,
    ENV_INFLIGHT_PER_WORKER: 8,
    ENV_TOOL_CALLS_PER_MIN: 60,
    ENV_READS_PER_MIN: 240,
    ENV_READS_PER_DAY: 4000,
    ENV_WRITES_PER_HOUR: 30,
    ENV_WRITES_PER_DAY: 150,
    ENV_MINTS_PER_HOUR: 10,
}

MINUTE = 60
HOUR = 60 * 60
DAY = 24 * 60 * 60
# A crashed worker never runs its `release`, so the in-flight counter expires on its own: the TTL is
# set once, when the counter is created, so it expires this long after the user's first in-flight
# request whatever happens after it. Longer than the 20s tool budget with room for a slow inner
# call, short enough to self-heal quickly.
_INFLIGHT_TTL_SECONDS = 120
_ACTIVITY_TTL_SECONDS = 8 * DAY
_CLIENT_NAME_TTL_SECONDS = 90 * DAY


def limit(env_var: str) -> int:
    """The configured value of one limit, or its default when unset or malformed.

    Args:
        env_var: One of the `ENV_*` names above.

    Returns:
        A non-negative integer; 0 means the limit refuses everything.
    """
    raw = (os.getenv(env_var) or "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = -1
        if value >= 0:
            return value
    return _DEFAULTS[env_var]


def _require_redis() -> Any:
    client = _redis()
    if client is None:
        raise LimitsUnavailable()
    return client


def _digest(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:length]


def _incr_with_ttl(client: Any, key: str, ttl_seconds: int) -> int:
    """Increment a counter that always carries a TTL. Raises whatever the client raises.

    `SET NX EX` creates the key with its TTL in one atomic command, and does nothing to an existing
    key, so the TTL is set once per counter and never refreshed by a later attempt. Then `INCR`,
    which keeps the TTL. If the `SET` fails nothing exists; if the `INCR` fails the key exists at 0
    with its TTL. Either way there is no window in which a counter exists without one.
    """
    client.set(key, 0, ex=ttl_seconds, nx=True)
    return int(client.incr(key))


# --- step 7: the pre-auth failure bucket ---------------------------------------------------------


def _address_failure_key(client_ip: Optional[str]) -> str:
    """The per-address bucket: every failed token from one address, whatever the token."""
    window = int(time.time() // MINUTE)
    return f"mcp:authfail-addr:{_digest(client_ip or '-')}:{window}"


def _failure_key(client_ip: Optional[str], token: str) -> str:
    """Keyed on the client address AND a short digest of the token presented.

    Keying on the address alone would let one bad token on a shared egress IP (an office, a CI
    runner, a VPN) lock every other user of that address out; the token digest keeps each
    presented credential in its own bucket. Neither value is stored in the clear.
    """
    window = int(time.time() // MINUTE)
    return f"mcp:authfail:{_digest(client_ip or '-')}:{_digest(token, 8)}:{window}"


def _over(key: str, cap: int) -> bool:
    """Has the counter at `key` reached `cap`? Fails closed: an unreadable count is "yes"."""
    if cap == 0:
        return True
    client = _require_redis()
    try:
        raw = client.get(key)
    except Exception as exc:  # noqa: BLE001 — every Redis fault fails closed here.
        raise LimitsUnavailable() from exc
    try:
        return int(raw or 0) >= cap
    except (TypeError, ValueError):
        return True


def address_failures_exceeded(client_ip: Optional[str]) -> bool:
    """Has this address already failed auth the per-address number of times this minute?

    The per-(address, token) bucket alone leaves a flood of RANDOM tokens unthrottled, because
    each new token starts its own bucket and costs a session lookup. This bucket counts every
    failure from the address, so the flood is refused before it reaches the database. It is set
    higher than the per-token one because an office or VPN egress address is shared.

    Raises:
        LimitsUnavailable: Redis could not be read.
    """
    return _over(_address_failure_key(client_ip), limit(ENV_ADDRESS_FAILURES_PER_MIN))


def auth_failures_exceeded(client_ip: Optional[str], token: str) -> bool:
    """Has this (address, token) pair already failed auth the configured times this minute?

    Raises:
        LimitsUnavailable: Redis could not be read.
    """
    return _over(_failure_key(client_ip, token), limit(ENV_FAILURES_PER_MIN))


def record_auth_failure(client_ip: Optional[str], token: Optional[str]) -> None:
    """Count one failed auth against the address and, given a token, the (address, token) pair.

    Best effort: a lost count is harmless, because the next request's `*_failures_exceeded` read is
    the control and fails closed on its own.

    Args:
        client_ip: The caller's address.
        token: The token presented, or None for a failure with no single token (a query string).
    """
    client = _redis()
    if client is None:
        return
    keys = [_address_failure_key(client_ip)]
    if token is not None:
        keys.append(_failure_key(client_ip, token))
    try:
        for key in keys:
            _incr_with_ttl(client, key, MINUTE + 5)
    except Exception as exc:  # noqa: BLE001
        log_debug("MCP auth-failure count not recorded", error_type=type(exc).__name__)


# --- step 10: concurrency -------------------------------------------------------------------------


class WorkerSlots:
    """The per-worker in-flight cap. One event loop per worker, so a plain counter is exact."""

    def __init__(self) -> None:
        """Start with nothing in flight."""
        self.in_flight = 0

    def try_acquire(self) -> bool:
        """Take a slot when one is free; never waits."""
        if self.in_flight >= limit(ENV_INFLIGHT_PER_WORKER):
            return False
        self.in_flight += 1
        return True

    def release(self) -> None:
        """Give a slot back."""
        self.in_flight = max(0, self.in_flight - 1)


worker_slots = WorkerSlots()


def _inflight_key(user_id: int) -> str:
    return f"mcp:inflight:{int(user_id)}"


def acquire_user_slot(user_id: int) -> bool:
    """Take one of this user's in-flight slots, across every worker. Blocking.

    Returns:
        True when a slot was taken (the caller MUST `release_user_slot`), False when the user is
        already at the cap.

    Raises:
        LimitsUnavailable: Redis could not be read or written.
    """
    cap = limit(ENV_INFLIGHT_PER_USER)
    if cap == 0:
        return False
    client = _require_redis()
    key = _inflight_key(user_id)
    try:
        # The TTL is set when the counter is created and never again: a refused attempt below, or a
        # slot that is never released, cannot extend the counter's life (`_incr_with_ttl`).
        current = _incr_with_ttl(client, key, _INFLIGHT_TTL_SECONDS)
        if current > cap:
            client.decr(key)
            return False
    except Exception as exc:  # noqa: BLE001
        raise LimitsUnavailable() from exc
    return True


def release_user_slot(user_id: int) -> None:
    """Give back a slot `acquire_user_slot` took. Best effort; the key's TTL is the backstop."""
    client = _redis()
    if client is None:
        return
    try:
        if int(client.decr(_inflight_key(user_id))) < 0:
            client.delete(_inflight_key(user_id))
    except Exception as exc:  # noqa: BLE001
        log_debug("MCP in-flight slot not released", error_type=type(exc).__name__)


# --- step 11: fixed-window budgets ---------------------------------------------------------------


@dataclass(frozen=True)
class Budget:
    """One fixed-window budget: `limit_env` calls per `window_seconds`, per user."""

    name: str
    limit_env: str
    window_seconds: int


TOOL_CALLS_PER_MIN = Budget("tool_calls_min", ENV_TOOL_CALLS_PER_MIN, MINUTE)
READS_PER_MIN = Budget("reads_min", ENV_READS_PER_MIN, MINUTE)
READS_PER_DAY = Budget("reads_day", ENV_READS_PER_DAY, DAY)
WRITES_PER_HOUR = Budget("writes_hour", ENV_WRITES_PER_HOUR, HOUR)
WRITES_PER_DAY = Budget("writes_day", ENV_WRITES_PER_DAY, DAY)

# Who consumes which: the GATE spends a tool-call unit per JSON-RPC `tools/call`; the in-process
# loopback (P3) spends the read budgets once per INNER call; the write tools (W2) spend the write
# budgets once per write. All are per user, across every token that user holds.
READ_BUDGETS = (READS_PER_MIN, READS_PER_DAY)
WRITE_BUDGETS = (WRITES_PER_HOUR, WRITES_PER_DAY)


def consume_budgets(user_id: int, budgets: Iterable[Budget]) -> Optional[int]:
    """Spend one unit of every budget in `budgets`, refusing if any is exhausted. Blocking.

    Returns:
        None when the call is within every budget, otherwise the `Retry-After` seconds of the
        first exhausted one. A budget configured to 0 refuses at once.

    Raises:
        LimitsUnavailable: Redis could not be read or written.
    """
    now = time.time()
    for budget in budgets:
        cap = limit(budget.limit_env)
        retry_after = max(1, int(budget.window_seconds - (now % budget.window_seconds)))
        if cap == 0:
            return retry_after
        client = _require_redis()
        key = f"mcp:budget:{budget.name}:{int(user_id)}:{int(now // budget.window_seconds)}"
        try:
            used = _incr_with_ttl(client, key, budget.window_seconds + 5)
        except Exception as exc:  # noqa: BLE001
            raise LimitsUnavailable() from exc
        if used > cap:
            return retry_after
    return None


# --- activity counts for the Agents tab (fail OPEN: a lost count is a number, not a control) -----


def _day(offset: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=offset)).strftime("%Y%m%d")


def record_activity(session_id: Optional[int], client_name: Optional[str],
                    tool_call: bool) -> None:
    """Count a tool call against its session for today, and remember the client's name.

    Args:
        session_id: The MCP session row id.
        client_name: The sanitised client name, or None.
        tool_call: Whether this request was a JSON-RPC `tools/call`.
    """
    client = _redis()
    if client is None or session_id is None:
        return
    try:
        if tool_call:
            _incr_with_ttl(client, f"mcp:calls:{int(session_id)}:{_day()}", _ACTIVITY_TTL_SECONDS)
        if client_name:
            client.set(f"mcp:client:{int(session_id)}", client_name, ex=_CLIENT_NAME_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        log_debug("MCP activity not recorded", error_type=type(exc).__name__)


def read_activity(session_ids: Iterable[int]) -> Optional[dict[int, dict[str, Any]]]:
    """`{session_id: {calls_7d, last_client_name}}` for the Agents tab, or None when unreadable.

    None, never zeros: an unreadable count is unknown, and the tab shows it as unknown.
    """
    client = _redis()
    ids = [int(s) for s in session_ids]
    if client is None:
        return None
    out: dict[int, dict[str, Any]] = {}
    try:
        for sid in ids:
            calls = 0
            for offset in range(7):
                raw = client.get(f"mcp:calls:{sid}:{_day(offset)}")
                calls += int(raw or 0)
            name = client.get(f"mcp:client:{sid}")
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            out[sid] = {"calls_7d": calls, "last_client_name": name or None}
    except Exception as exc:  # noqa: BLE001
        log_debug("MCP activity unreadable", error_type=type(exc).__name__)
        return None
    return out


# --- token mint rate (fail OPEN; the 10-live-token cap is a separate database check) ------------


def claim_mint(user_id: int) -> bool:
    """Spend one of this user's mints for the hour. True when allowed.

    Fails open: with Redis unreachable the mint proceeds, because it is already behind a human
    step-up ceremony and the 10-live-token cap, which `create_session_within_cap` checks and
    enforces in one database transaction under the user's row lock, without Redis.
    """
    cap = limit(ENV_MINTS_PER_HOUR)
    if cap == 0:
        return False
    client = _redis()
    if client is None:
        log_debug("MCP mint limiter unavailable — allowing the mint", user_id=user_id)
        return True
    key = f"mcp:mints:{int(user_id)}:{int(time.time() // HOUR)}"
    try:
        used = _incr_with_ttl(client, key, HOUR + 5)
    except Exception as exc:  # noqa: BLE001
        log_debug("MCP mint limiter failed — allowing the mint", user_id=user_id,
                  error_type=type(exc).__name__)
        return True
    return used <= cap
