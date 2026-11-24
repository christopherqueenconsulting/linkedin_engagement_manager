"""The write path every MCP write tool runs through (W2, `docs/agent-mcp-plan.md` §3.3, §4, §5, §7).

In order, before and around the tool's own inner call:

1. **Rollout flag** `mcp-server-writes`: off -> `isError` `writes_disabled`. A rollout control,
   never the safety one (that is the gate's kill switch, checked first on every request).
2. **Approval account** (owner ruling O1): a tool marked `approval` runs only on an internal or
   sandbox account -> else `agent_may_not_approve`. The route decides it again on its own.
3. **Dedupe**: the key `mcp:dedupe:{user}:{session}:{tool}:{client_request_id}` is claimed in Redis
   with `SET NX EX 600` BEFORE anything is dispatched, with `sha256(canonical args)` stored in it.
   With no `client_request_id` the id is derived from that hash, so an identical retry is still
   caught. A hit with a different hash is `client_request_id_reused`; a hit on a finished write
   replays its answer (`replayed: true`) and writes nothing; a hit on one still running, or whose
   outcome is unknown, is `outcome=unknown`. Redis unreadable -> `mcp_unavailable` (fails CLOSED,
   like every gate limit).
4. **Write budgets**: 30 an hour and 150 a day per user (owner ruling O10), spent once per write.
5. **Run** under the tool time limit. A timeout is `outcome=unknown`, NOT retryable: the inner
   handler's thread keeps running, so the write may land after the answer. The claim is kept, so a
   blind retry is refused rather than doubled. A refused write (4xx) changed nothing and releases
   the claim; a 5xx is treated like a timeout.
6. **Audit**: one `mcp_write {tool, target_id, scope}` row per write that landed, never a body.

Logging follows §7: a refusal here is the guard working, so it is DEBUG (flag off, budget, dedupe
hit) or INFO (an approval refused on a standard account). Nothing here logs arguments or results.
"""

import asyncio
import hashlib
import json
from typing import Any, Awaitable, Callable, Optional

from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from cqc_lem.api.mcp import limits
from cqc_lem.api.mcp.limits import WRITE_BUDGETS, LimitsUnavailable, consume_budgets
from cqc_lem.api.mcp.loopback import (
    INVALID_ARGS,
    RATE_LIMITED,
    REFUSED,
    UNKNOWN,
    UPSTREAM_ERROR,
    ToolFailure,
)
from cqc_lem.utilities.db import (
    INTERNAL_ACCOUNT_KINDS,
    AuthAuditEvent,
    get_user_account_kind,
    record_auth_event,
)
from cqc_lem.utilities.flags import MCP_SERVER_WRITES, flag_enabled
from cqc_lem.utilities.logger import log_debug, log_info

DEDUPE_TTL_SECONDS = 600
_PENDING, _DONE, _UNKNOWN = "pending", "done", "unknown"

UNKNOWN_MESSAGE = ("LEM did not confirm this write in time, so it may or may not have happened. "
                   "Do NOT retry it: check lem_list_posts or lem_list_outreach first.")


def args_hash(args: BaseModel) -> str:
    """`sha256` of the canonical arguments, without the client's own request id."""
    data = args.model_dump(mode="json", exclude={"client_request_id"})
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def dedupe_key(user_id: int, session_id: Any, tool: str, request_id: str) -> str:
    """The Redis key one write is claimed under. Per user AND per session, so no two users share one."""
    return f"mcp:dedupe:{int(user_id)}:{session_id}:{tool}:{request_id}"


def _redis() -> Optional[Any]:
    """The limits module's Redis client, looked up at call time (tests and a changed URL)."""
    return limits._redis()


def _decode(raw: Any) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        record = json.loads(raw)
    except (TypeError, ValueError):
        return {"state": _UNKNOWN}
    return record if isinstance(record, dict) else {"state": _UNKNOWN}


def claim_write(key: str, digest: str) -> Optional[dict[str, Any]]:
    """Claim `key` for a new write. Blocking: call it through `run_in_threadpool`.

    Returns:
        None when this call now holds the claim; otherwise the record already stored under it.

    Raises:
        LimitsUnavailable: Redis could not be read or written.
    """
    client = _redis()
    if client is None:
        raise LimitsUnavailable()
    fresh = json.dumps({"h": digest, "state": _PENDING})
    try:
        if client.set(key, fresh, ex=DEDUPE_TTL_SECONDS, nx=True):
            return None
        raw = client.get(key)
        if raw is None and client.set(key, fresh, ex=DEDUPE_TTL_SECONDS, nx=True):
            return None  # it expired between the two commands
    except Exception as exc:  # noqa: BLE001 — every Redis fault fails closed here.
        raise LimitsUnavailable() from exc
    return _decode(raw)


def settle_write(key: str, digest: str, state: str, result: Optional[dict[str, Any]] = None) -> None:
    """Record how a claimed write ended. Best effort: a lost record only weakens a later replay."""
    client = _redis()
    if client is None:
        return
    try:
        client.set(key, json.dumps({"h": digest, "state": state, "result": result}, default=str),
                   ex=DEDUPE_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        log_debug("MCP write outcome not recorded", error_type=type(exc).__name__)


def release_write(key: str) -> None:
    """Drop a claim whose write changed nothing, so a corrected retry can run. Best effort."""
    client = _redis()
    if client is None:
        return
    try:
        client.delete(key)
    except Exception as exc:  # noqa: BLE001
        log_debug("MCP write claim not released", error_type=type(exc).__name__)


def account_may_approve(user_id: int) -> bool:
    """Owner ruling O1: True only for an internal or sandbox account. Blocking; fails closed.

    `get_user_account_kind` answers 'standard' when the row cannot be read, so an unreadable
    account never approves. The same set `_operator_may_approve` reads.
    """
    return get_user_account_kind(user_id) in INTERNAL_ACCOUNT_KINDS


def _audit_write(principal: Any, tool: str, target_id: Any) -> None:
    try:
        record_auth_event(AuthAuditEvent.MCP_WRITE, user_id=principal.user_id,
                          session_id=principal.session_id,
                          details={"tool": tool, "target_id": target_id, "scope": principal.scope})
    except Exception as exc:  # noqa: BLE001 — the write landed; a lost audit row must not undo it.
        log_debug("MCP write audit row not written", error_type=type(exc).__name__)


async def _check_preconditions(spec: Any, principal: Any) -> None:
    user_id = principal.user_id
    if not await run_in_threadpool(flag_enabled, MCP_SERVER_WRITES, user_id):
        log_debug("MCP write refused: writes flag off", user_id=user_id, tool=spec.name)
        raise ToolFailure(REFUSED, "writes_disabled",
                          "Writes are not enabled for this account. Ask the user to make the "
                          "change in LEM (lem_open_in_lem).")
    if spec.approval and not await run_in_threadpool(account_may_approve, user_id):
        log_info("MCP approval refused: not an internal or sandbox account", user_id=user_id,
                 tool=spec.name)
        raise ToolFailure(REFUSED, "agent_may_not_approve",
                          "This token can queue work for review but cannot approve it.")


async def _claim(key: str, digest: str, spec: Any, principal: Any) -> Optional[dict[str, Any]]:
    """Claim the dedupe key; return a replayed answer, or None when this call should write."""
    try:
        existing = await run_in_threadpool(claim_write, key, digest)
    except LimitsUnavailable as exc:
        raise ToolFailure(UPSTREAM_ERROR, "mcp_unavailable",
                          "The LEM MCP server is unavailable right now. Try again shortly.") from exc
    if existing is None:
        return None
    log_debug("MCP write dedupe hit", user_id=principal.user_id, tool=spec.name)
    if existing.get("h") != digest:
        raise ToolFailure(INVALID_ARGS, "client_request_id_reused",
                          "client_request_id reused with different arguments.")
    if existing.get("state") == _DONE and isinstance(existing.get("result"), dict):
        return {**existing["result"], "replayed": True}
    raise ToolFailure(UNKNOWN, "duplicate_outcome_unknown",
                      "This write is already in progress or its outcome is unknown. " + UNKNOWN_MESSAGE)


async def _spend_budget(key: str, principal: Any) -> None:
    try:
        retry_after = await run_in_threadpool(consume_budgets, principal.user_id, WRITE_BUDGETS)
    except LimitsUnavailable as exc:
        await run_in_threadpool(release_write, key)
        raise ToolFailure(UPSTREAM_ERROR, "mcp_unavailable",
                          "The LEM MCP server is unavailable right now. Try again shortly.") from exc
    if retry_after is not None:
        await run_in_threadpool(release_write, key)
        log_debug("MCP write refused: write budget spent", user_id=principal.user_id)
        raise ToolFailure(RATE_LIMITED, "write_budget_exhausted",
                          f"This account's MCP write budget is spent. Retry in {retry_after}s.")


async def run_write(spec: Any, args: BaseModel, principal: Any,
                    run: Callable[[], Awaitable[dict[str, Any]]], timeout: float) -> dict[str, Any]:
    """Run one write tool call through the flag, dedupe, budgets, time limit and audit.

    Args:
        spec: The tool's `ToolSpec` (`write=True`).
        args: Its validated arguments.
        principal: The admitted `McpPrincipal`.
        run: Starts the tool's runner (the inner write).
        timeout: The tool time limit, in seconds.

    Returns:
        The tool's answer, or the first answer replayed for a duplicate.

    Raises:
        ToolFailure: Refused, deduplicated, rate-limited, or the outcome is unknown.
    """
    await _check_preconditions(spec, principal)
    digest = args_hash(args)
    request_id = getattr(args, "client_request_id", None) or digest[:32]
    key = dedupe_key(principal.user_id, principal.session_id, spec.name, request_id)
    replay = await _claim(key, digest, spec, principal)
    if replay is not None:
        return replay
    await _spend_budget(key, principal)
    try:
        payload = await asyncio.wait_for(run(), timeout)
    except asyncio.TimeoutError:
        await run_in_threadpool(settle_write, key, digest, _UNKNOWN)
        raise ToolFailure(UNKNOWN, "outcome_unknown", UNKNOWN_MESSAGE) from None
    except ToolFailure as failure:
        if failure.outcome == UPSTREAM_ERROR:
            await run_in_threadpool(settle_write, key, digest, _UNKNOWN)
        else:
            await run_in_threadpool(release_write, key)
        raise
    except Exception:
        await run_in_threadpool(settle_write, key, digest, _UNKNOWN)
        raise
    await run_in_threadpool(settle_write, key, digest, _DONE, payload)
    await run_in_threadpool(_audit_write, principal, spec.name, payload.get("id"))
    return payload


__all__ = ["run_write", "args_hash", "dedupe_key", "claim_write", "settle_write", "release_write",
           "account_may_approve", "DEDUPE_TTL_SECONDS", "UNKNOWN_MESSAGE"]
