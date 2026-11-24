"""`McpGate`: the pure-ASGI front door of `POST /api/mcp` (`docs/agent-mcp-plan.md` §3.3).

Every check is the gate's own and answers JSON, in a fixed order, BEFORE the SDK sees the
request: the SDK's defaults answer plain text and are not what decides who gets in. The plan's
steps 1-6, with the kill switch split so every check that needs no I/O runs before the Redis read:

1. Kill switch, no-I/O half: `MCP_SERVER_KILL` -> 503 `mcp_disabled_by_operator`; a failed
   lifespan or an empty/invalid `PUBLIC_BASE_URL` -> 503 `mcp_unavailable`.
2. Method: anything but POST -> 405 with `Allow: POST`.
3. Host must be `PUBLIC_BASE_URL`'s (plus loopback on a loopback base) -> else 421; an Origin, when
   present, must be its origin -> else 403.
4. Content-Type must be `application/json` -> else 415; a declared length over 256 KB -> 413.
5. Kill switch, Redis half: the `mcp:kill` key -> 503 `mcp_disabled_by_operator`; an unreadable
   Redis -> 503 `mcp_unavailable` (fails closed). Before the body and before any auth step.
6. A streamed body over 256 KB -> 413; a client gone mid-body still gets a (400) response.
7. A token in the query string (`token`, `session_token`, `access_token`) -> 400
   `token_in_url_refused`. A value that resolves to ANY session is revoked at once, audited as
   `mcp_token_exposed` and its owner emailed (WARNING); one that resolves to nothing is INFO and
   counts against the per-address bucket, which is checked BEFORE any lookup (at most 4 values).
8. Only `Authorization: Bearer` is read; an outer `lem_session` cookie is ignored, and a cookie with
   no bearer -> 401 `cookie_only`.
9. Pre-auth failure buckets: per client address (60 failures a minute, so a flood of random tokens
   is refused before the database) and per (address, token digest) (30 a minute) -> 429.
10. Resolve: a database fault -> 503 `mcp_unavailable`; unknown, expired or revoked -> 401
   `invalid_token` (DEBUG); a live session of any other scope -> the same 401 (WARNING). The
   lookup writes nothing for a non-MCP session: its expiry and `last_seen_at` are left as they were.
11. Rollout flag `mcp-server-enabled` off for the user -> 403 `mcp_disabled`.
12. Concurrency: 2 in flight per user (Redis), 8 per worker (in process) -> 429 with `Retry-After`.
13. Budget: each JSON-RPC `tools/call` spends the per-user tool-call budget -> 429 when exhausted.
     The inner-read budgets are spent by the loopback (P3), the write budgets by the write tools (W2).
14. Principal: `McpPrincipal` into `scope["state"]["mcp_principal"]`.
15. Access: the once-a-day `mcp_access` audit row, and the Agents tab's activity counts.

Steps 9-15 are the plan's §3.3 steps 7-13 (P2).

Then the request goes to the SDK's session manager with the body the gate already read. Every
blocking call (resolve, Redis, flag, audit) runs in the thread pool. Steps 12-15 and the SDK call
sit inside ONE try/finally, so a slot is returned on every exit, a cancellation included.

This is the ONE public `/api` path that resolves a session, and it admits only the two MCP scopes
(`docs/identity-and-sessions.md`, the MCP carve-out). The token is never logged, put in a response
or put in exception text.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Generator, MutableMapping, Optional

import anyio
from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import Headers, QueryParams
from starlette.requests import Request
from starlette.responses import JSONResponse

from cqc_lem.api.mcp.lifespan import MAX_REQUEST_BODY_BYTES, MCP_PATH, McpState, mcp_state
from cqc_lem.api.mcp.limits import (
    TOOL_CALLS_PER_MIN,
    KillState,
    LimitsUnavailable,
    acquire_user_slot,
    address_failures_exceeded,
    auth_failures_exceeded,
    consume_budgets,
    kill_env_set,
    read_kill_key,
    record_activity,
    record_auth_failure,
    release_user_slot,
    worker_slots,
)
from cqc_lem.api.mcp.server import FREE_RESOURCE_URIS, mcp_server
from cqc_lem.utilities.db import (
    MCP_SESSION_SCOPES,
    AuthAuditEvent,
    SessionStoreUnavailable,
    record_auth_event,
    resolve_session_checked,
    revoke_session_by_token,
)
from cqc_lem.utilities.env_constants import SESSION_COOKIE_NAME
from cqc_lem.utilities.flags import MCP_SERVER, flag_enabled
from cqc_lem.utilities.logger import log_debug, log_info, log_warning
from cqc_lem.utilities.observability import track_mcp_auth_refused
from cqc_lem.utilities.observer_access import claim_mcp_access

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

QUERY_TOKEN_PARAMS = ("token", "session_token", "access_token")

INVALID_TOKEN_DETAIL = ("LEM MCP token invalid, expired or revoked. "
                        "Create a new one in LEM → Agents.")
MISSING_TOKEN_DETAIL = ("This endpoint needs an `Authorization: Bearer` LEM MCP token. "
                        "Create one in LEM → Agents.")
COOKIE_ONLY_DETAIL = ("A LEM browser session is not an MCP credential. Send an "
                      "`Authorization: Bearer` LEM MCP token instead; create one in LEM → Agents.")


class _BodyTooLarge(Exception):
    """The streamed request body passed `MAX_REQUEST_BODY_BYTES`."""


class _ClientGone(Exception):
    """The client disconnected before its body finished arriving."""


# The `mcp_auth_refused.reason` each refusal is reported as (plan §7). A wrong-scope token answers
# the same 401 body as an invalid one, so its event reason is passed explicitly (`_invalid_token`).
_EVENT_REASONS = {
    "mcp_disabled_by_operator": "killed", "mcp_unavailable": "unavailable",
    "method_not_allowed": "method", "host_not_allowed": "host", "origin_not_allowed": "origin",
    "unsupported_media_type": "content_type", "request_too_large": "too_large",
    "invalid_content_length": "bad_request", "client_disconnected": "bad_request",
    "token_in_url_refused": "query_token", "cookie_only": "cookie_only",
    "missing_token": "missing", "invalid_token": "invalid", "rate_limited": "rate_limited",
    "concurrency_limited": "concurrency", "mcp_disabled": "flag_off",
}


def _refusal(status_code: int, reason: str, detail: str,
             headers: Optional[dict[str, str]] = None,
             event_reason: Optional[str] = None) -> JSONResponse:
    """The gate's one response shape: `{status_code, detail, reason}`, the house refusal body.

    The response also carries the reason it is reported under, read by `McpGate.dispatch` when it
    emits `mcp_auth_refused`.
    """
    response = JSONResponse(status_code=status_code, headers=headers,
                            content={"status_code": status_code, "detail": detail, "reason": reason})
    response.mcp_event_reason = event_reason or _EVENT_REASONS.get(reason, reason)  # type: ignore[attr-defined]
    return response


def _report_refusal(response: JSONResponse) -> None:
    """Emit `mcp_auth_refused` for a refusal. Never raises (the tracker swallows its own faults)."""
    track_mcp_auth_refused(getattr(response, "mcp_event_reason", None) or "unknown")


def _normalise_host(host: str, state: McpState) -> str:
    """Lower-case a Host header and drop the default port of the configured scheme."""
    host = host.strip().lower()
    default_port = ":443" if state.origin.startswith("https://") else ":80"
    return host[: -len(default_port)] if host.endswith(default_port) else host


def _host_allowed(host: Optional[str], state: McpState) -> bool:
    """Is this Host header the configured public host (or loopback, on a loopback base)?"""
    if not host:
        return False
    host = _normalise_host(host, state)
    if host == state.host:
        return True
    if not state.dev_loopback:
        return False
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    return name in ("localhost", "127.0.0.1", "[::1]")


def _origin_allowed(origin: Optional[str], state: McpState) -> bool:
    """An absent Origin is a non-browser client and passes; a present one must match exactly."""
    if origin is None:
        return True
    origin = origin.strip().lower().rstrip("/")
    if origin == state.origin:
        return True
    if not state.dev_loopback:
        return False
    for loopback in ("http://localhost", "http://127.0.0.1", "http://[::1]"):
        if origin == loopback or origin.startswith(loopback + ":"):
            return True
    return False


def _content_type_is_json(content_type: Optional[str]) -> bool:
    """`application/json`, optionally with parameters such as `; charset=utf-8`."""
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower() == "application/json"


def _bearer(authorization: Optional[str]) -> Optional[str]:
    """The bearer token, parsed by the same rule the edge credential gate uses."""
    from cqc_lem.api.main import _bearer_token  # lazy: main imports this module

    return _bearer_token(authorization)


async def _read_body(receive: Receive) -> bytes:
    """Read the whole request body, refusing once it passes the cap rather than after.

    Raises:
        _BodyTooLarge: The body passed `MAX_REQUEST_BODY_BYTES`.
        _ClientGone: The client disconnected mid-body.
    """
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise _ClientGone()
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > MAX_REQUEST_BODY_BYTES:
            raise _BodyTooLarge()
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


_KILLED_DETAIL = "The LEM MCP server is switched off by the operator."
_UNAVAILABLE_DETAIL = "The LEM MCP server is unavailable right now. Try again shortly."


def _local_kill_refusal(state: McpState) -> Optional[JSONResponse]:
    """Step 1, the half with no I/O: the env kill switch, then whether the lifespan came up.

    First of all, because the Host/Origin checks below need the base URL the lifespan read.
    """
    if kill_env_set():
        log_debug("MCP request refused: kill switch on", source="env")
        return _refusal(503, "mcp_disabled_by_operator", _KILLED_DETAIL)
    if state.unavailable:
        log_debug("MCP request refused: endpoint unavailable", cause=state.reason)
        return _refusal(503, "mcp_unavailable", _UNAVAILABLE_DETAIL)
    return None


async def _redis_kill_refusal() -> Optional[JSONResponse]:
    """Step 1, the Redis half: the `mcp:kill` key, failing closed when it cannot be read.

    Runs AFTER the header-only checks, so junk traffic never holds a threadpool thread on Redis, and
    BEFORE the body read and every auth step, so it still refuses everything that would get further.
    """
    kill = await run_in_threadpool(read_kill_key)
    if kill is KillState.ON:
        log_debug("MCP request refused: kill switch on", source="redis")
        return _refusal(503, "mcp_disabled_by_operator", _KILLED_DETAIL)
    if kill is KillState.UNREADABLE:
        log_debug("MCP request refused: endpoint unavailable", cause="kill_key_unreadable")
        return _refusal(503, "mcp_unavailable", _UNAVAILABLE_DETAIL)
    return None


def _http_refusal(scope: Scope, headers: Headers, state: McpState) -> Optional[JSONResponse]:
    """Steps 2-4 that need no body: method, Host, Origin, Content-Type, declared length."""
    if scope.get("method") != "POST":
        log_debug("MCP request refused: method", method=scope.get("method"))
        return _refusal(405, "method_not_allowed", "Only POST is served at /api/mcp.",
                        headers={"Allow": "POST"})
    if not _host_allowed(headers.get("host"), state):
        log_debug("MCP request refused: host")
        return _refusal(421, "host_not_allowed", "This request was sent to the wrong host.")
    if not _origin_allowed(headers.get("origin"), state):
        log_debug("MCP request refused: origin")
        return _refusal(403, "origin_not_allowed", "Cross-origin requests are not accepted.")
    if not _content_type_is_json(headers.get("content-type")):
        log_debug("MCP request refused: content type")
        return _refusal(415, "unsupported_media_type", "Content-Type must be application/json.")
    declared = headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            return _refusal(400, "invalid_content_length", "Content-Length is not a number.")
        if length > MAX_REQUEST_BODY_BYTES:
            log_debug("MCP request refused: body too large", declared=True)
            return _refusal(413, "request_too_large", "The request body is over 256 KB.")
    return None


def _client_ip(scope: Scope) -> Optional[str]:
    """The caller's address, by the same CF-Connecting-IP-first rule every `/api` route uses."""
    from cqc_lem.api.main import _client_ip as main_client_ip  # lazy: main imports this module

    return main_client_ip(Request(scope))


def _user_agent(scope: Scope) -> Optional[str]:
    return Headers(scope=scope).get("user-agent")


# --- step 5: a token in the query string ----------------------------------------------------------

# A request carrying more than this many candidate values is not a client mistake; the rest are
# refused with the same 400 and not looked up, so one request cannot become a query storm.
_MAX_EXPOSED_VALUES = 4


def _revoke_exposed(scope: Scope, query: QueryParams,
                    params: list[str]) -> tuple[list[dict[str, Any]], int]:
    """Revoke every live session a query-string value names, and audit and email each. Blocking.

    A token in a URL is in a log somewhere already (a proxy, a terminal, shell history), so it is
    treated as exposed whatever its scope: revoked at once, audited as `mcp_token_exposed`, and its
    owner emailed. Never logs or returns the value. At most `_MAX_EXPOSED_VALUES` are looked up.

    Returns:
        `(revoked, misses)`: one entry per revoked session, and how many values matched nothing.
    """
    from cqc_lem.api.mcp.notify import notify_token_event

    values = [(name, v.strip()) for name in params for v in query.getlist(name) if v.strip()]
    revoked: list[dict[str, Any]] = []
    misses = 0
    for param, value in values[:_MAX_EXPOSED_VALUES]:
        found = revoke_session_by_token(value)
        if not found:
            misses += 1
            continue
        record_auth_event(AuthAuditEvent.MCP_TOKEN_EXPOSED, user_id=found["user_id"],
                          session_id=found["session_id"], ip=_client_ip(scope),
                          user_agent=_user_agent(scope),
                          details={"scope": found["scope"], "param": param})
        notify_token_event(found["user_id"], "exposed", label=found.get("label"),
                           scope=found["scope"])
        revoked.append({"user_id": found["user_id"], "scope": found["scope"]})
    return revoked, misses


async def _query_token_refusal(scope: Scope) -> Optional[JSONResponse]:
    """Step 7. Any token parameter is refused; one that resolves is revoked on the spot.

    The lookups run under the per-address failure bucket: an address already over it gets 429 and
    no lookup at all, and every value that matches nothing counts against it, so the query string
    cannot be used to walk the sessions table either.
    """
    query = QueryParams(scope.get("query_string", b"").decode("latin-1"))
    params = sorted(name for name in QUERY_TOKEN_PARAMS if name in query)
    if not params:
        return None
    client_ip = _client_ip(scope)
    try:
        if await run_in_threadpool(address_failures_exceeded, client_ip):
            log_debug("MCP request refused: too many failed attempts from this address")
            return _rate_limited("rate_limited", 60)
    except LimitsUnavailable:
        log_debug("MCP request refused: endpoint unavailable", cause="limits_unreadable")
        return _unavailable()
    revoked, misses = await run_in_threadpool(_revoke_exposed, scope, query, params)
    for _ in range(misses):
        await run_in_threadpool(record_auth_failure, client_ip, None)
    if revoked:
        # WARNING, never the value: a live credential was sent in a URL, and its owner has to
        # create a new one. Unlike a typo'd parameter, this is worth a grouped issue if it recurs.
        log_warning("MCP request refused: exposed token in query string was revoked",
                    params=params, revoked=len(revoked),
                    user_ids=sorted({r["user_id"] for r in revoked}),
                    scopes=sorted({r["scope"] for r in revoked}))
    else:
        log_info("MCP request refused: token in query string", params=params)
    return _refusal(400, "token_in_url_refused",
                    "Never put a token in a URL. Send it as `Authorization: Bearer`, and "
                    "rotate it in LEM → Agents if it was ever shared.")


# --- step 6: the bearer ---------------------------------------------------------------------------


def _bearer_refusal(headers: Headers) -> tuple[Optional[str], Optional[JSONResponse]]:
    """Step 6. Return `(token, None)`, or `(None, refusal)` when there is no bearer."""
    token = _bearer(headers.get("authorization"))
    if token is not None:
        return token, None
    cookies = [c.split("=", 1)[0].strip() for c in (headers.get("cookie") or "").split(";")]
    if SESSION_COOKIE_NAME in cookies:
        log_debug("MCP request refused: browser cookie without a bearer")
        return None, _refusal(401, "cookie_only", COOKIE_ONLY_DETAIL,
                              headers={"WWW-Authenticate": "Bearer"})
    log_debug("MCP request refused: no bearer token")
    return None, _refusal(401, "missing_token", MISSING_TOKEN_DETAIL,
                          headers={"WWW-Authenticate": "Bearer"})


def _invalid_token(event_reason: str = "invalid") -> JSONResponse:
    """The one 401 for a bearer that is not a live MCP session — never says which way it failed.

    The body is identical either way; only the internal `mcp_auth_refused` reason differs.
    """
    return _refusal(401, "invalid_token", INVALID_TOKEN_DETAIL,
                    headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
                    event_reason=event_reason)


def _unavailable() -> JSONResponse:
    return _refusal(503, "mcp_unavailable",
                    "The LEM MCP server is unavailable right now. Try again shortly.")


def _rate_limited(reason: str, retry_after: int) -> JSONResponse:
    return _refusal(429, reason, "Too many requests to the LEM MCP server. Slow down and retry.",
                    headers={"Retry-After": str(max(1, int(retry_after)))})


# --- the JSON-RPC body, read only for what the gate needs ------------------------------------------

_CLIENT_NAME_CHARS = re.compile(r"[^A-Za-z0-9 ._/()\-]")
_TOOL_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.\-]")
_METHOD_CHARS = re.compile(r"[^A-Za-z0-9_./\-]")
CLIENT_NAME_MAX = 40


def _clean(value: Any, pattern: "re.Pattern[str]", length: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = pattern.sub("", value).strip()[:length].strip()
    return cleaned or None


@dataclass(frozen=True)
class RpcSummary:
    """What the gate reads out of a JSON-RPC body: never arguments, never results."""

    tool_calls: int = 0
    first_tool: Optional[str] = None
    first_method: Optional[str] = None
    client_info_name: Optional[str] = None


def _rpc_summary(body: bytes) -> RpcSummary:
    """Count `tools/call` messages and pick out the client's self-reported name, if any.

    The SDK validates the body properly; this only has to be safe on anything, so a body that is
    not JSON-RPC is simply an empty summary and the SDK answers it.
    """
    try:
        parsed = json.loads(body or b"null")
    except (ValueError, UnicodeDecodeError):
        return RpcSummary()
    messages = parsed if isinstance(parsed, list) else [parsed]
    tool_calls, first_tool, first_method, client_name = 0, None, None, None
    for message in messages:
        if not isinstance(message, dict):
            continue
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if first_method is None and isinstance(method, str):
            first_method = _clean(method, _METHOD_CHARS, 64)
        if method == "tools/call":
            tool_calls += 1
            first_tool = first_tool or _clean(params.get("name"), _TOOL_NAME_CHARS, 64)
        elif method == "resources/read" and params.get("uri") not in FREE_RESOURCE_URIS:
            # A data resource runs the same tool a `tools/call` would (`calls.read_resource`), so it
            # spends the same budget. Only the two static text resources are free.
            tool_calls += 1
        elif method == "initialize":
            info = params.get("clientInfo")
            if isinstance(info, dict):
                client_name = client_name or _clean(info.get("name"), _CLIENT_NAME_CHARS,
                                                    CLIENT_NAME_MAX)
    return RpcSummary(tool_calls, first_tool, first_method, client_name)


def _client_name(summary: RpcSummary, headers: Headers) -> Optional[str]:
    """`clientInfo.name` on an initialize, else a sanitised User-Agent (at most 40 characters).

    Stateless calls after `initialize` carry no `clientInfo`, so the User-Agent is what names the
    client on every other request.
    """
    return summary.client_info_name or _clean(headers.get("user-agent"), _CLIENT_NAME_CHARS,
                                              CLIENT_NAME_MAX)


# --- steps 7-13: who is calling, and may they -------------------------------------------------------


@dataclass(frozen=True)
class McpPrincipal:
    """The authenticated caller of one MCP request, put in `scope["state"]["mcp_principal"]`.

    Carries no token. The P3 tools read it to dispatch the loopback and to label results.
    """

    user_id: int
    session_id: Optional[int]
    scope: str
    expires_at: Optional[datetime]
    client_name: Optional[str]


@dataclass
class _Admission:
    """An authenticated request: who it is, and the concurrency slots it holds until it finishes.

    The flags are set the moment a slot is taken, so `_release` gives back exactly what is held, from
    the `finally` in `McpGate.dispatch`, whatever ended the request: a refusal, an exception or a
    cancellation.
    """

    principal: McpPrincipal
    holds_user_slot: bool = False
    holds_worker_slot: bool = False


async def _release(admission: _Admission) -> None:
    """Give back every slot the admission holds. Safe on any exit path, cancellation included.

    The worker slot is given back first and synchronously, so no cancellation can interrupt it. The
    Redis slot needs a thread, so that call is shielded: a request cancelled mid-release still
    returns it rather than leaving it to the counter's TTL.
    """
    if admission.holds_worker_slot:
        admission.holds_worker_slot = False
        worker_slots.release()
    if admission.holds_user_slot:
        admission.holds_user_slot = False
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(release_user_slot, admission.principal.user_id)


def _record_access(principal: McpPrincipal, token: str, summary: RpcSummary) -> None:
    """Step 15: the once-a-day `mcp_access` row and the Agents tab's activity counts. Never raises."""
    from cqc_lem.api.main import _record_daily_access  # lazy: main imports this module

    _record_daily_access(AuthAuditEvent.MCP_ACCESS, principal.scope, claim_mcp_access,
                         principal.user_id, token, MCP_PATH,
                         {"path": MCP_PATH, "tool": summary.first_tool or summary.first_method,
                          "client_name": principal.client_name})
    record_activity(principal.session_id, principal.client_name, summary.tool_calls > 0)


async def _authenticate(scope: Scope, headers: Headers, token: str,
                        summary: RpcSummary) -> tuple[Optional[McpPrincipal], Optional[JSONResponse]]:
    """Steps 9-11: who is calling, and may they. Holds nothing, so a refusal has nothing to release.

    Returns:
        `(principal, None)` for an admitted MCP session, or `(None, refusal)`.
    """
    client_ip = _client_ip(scope)
    try:
        # Per address first: a flood of random tokens starts a fresh per-token bucket each time, so
        # only this one stops it before the session lookup.
        if await run_in_threadpool(address_failures_exceeded, client_ip):
            log_debug("MCP request refused: too many failed attempts from this address")
            return None, _rate_limited("rate_limited", 60)
        if await run_in_threadpool(auth_failures_exceeded, client_ip, token):
            log_debug("MCP request refused: too many failed attempts")
            return None, _rate_limited("rate_limited", 60)
    except LimitsUnavailable:
        log_debug("MCP request refused: endpoint unavailable", cause="limits_unreadable")
        return None, _unavailable()

    try:
        # Writes only for an MCP session: any other session is refused below, and must leave this
        # lookup exactly as it came in (its idle window unslid, `last_seen_at` untouched).
        resolved = await run_in_threadpool(resolve_session_checked, token,
                                           mutate_scopes=MCP_SESSION_SCOPES)
    except SessionStoreUnavailable:
        # The repository already logged the fault at ERROR. 503, never "create a new token".
        log_debug("MCP request refused: endpoint unavailable", cause="session_store")
        return None, _unavailable()
    if not resolved:
        await run_in_threadpool(record_auth_failure, client_ip, token)
        log_debug("MCP request refused: bearer does not resolve to an MCP session")
        return None, _invalid_token()
    if resolved.get("scope") not in MCP_SESSION_SCOPES:
        await run_in_threadpool(record_auth_failure, client_ip, token)
        # WARNING: a live NON-MCP credential (a browser session, an agent seat) was sent to the MCP
        # endpoint. No MCP client does that by accident; the answer is the same 401 regardless.
        log_warning("MCP request refused: token is not an MCP session",
                    user_id=resolved.get("user_id"), scope=resolved.get("scope"))
        return None, _invalid_token("wrong_scope")

    user_id = int(resolved["user_id"])
    if not await run_in_threadpool(flag_enabled, MCP_SERVER, user_id):
        log_debug("MCP request refused: rollout flag off", user_id=user_id)
        return None, _refusal(403, "mcp_disabled",
                              "The LEM MCP server is not enabled for this account yet.")
    return McpPrincipal(user_id=user_id, session_id=resolved.get("session_id"),
                        scope=resolved["scope"], expires_at=resolved.get("expires_at"),
                        client_name=_client_name(summary, headers)), None


async def _take_slots(admission: _Admission, summary: RpcSummary) -> Optional[JSONResponse]:
    """Steps 12-13: concurrency, then the tool-call budget. None when the request may proceed.

    Only ever called inside `dispatch`'s try/finally, which releases whatever this took. Each flag
    is set with no `await` between taking the slot and recording it, and the Redis acquire is
    shielded, so a cancellation cannot land between the two and strand a slot.
    """
    user_id = admission.principal.user_id
    if not worker_slots.try_acquire():
        log_debug("MCP request refused: worker concurrency", user_id=user_id)
        return _rate_limited("concurrency_limited", 1)
    admission.holds_worker_slot = True
    try:
        with anyio.CancelScope(shield=True):
            admission.holds_user_slot = await run_in_threadpool(acquire_user_slot, user_id)
        if not admission.holds_user_slot:
            log_debug("MCP request refused: user concurrency", user_id=user_id)
            return _rate_limited("concurrency_limited", 1)
        if summary.tool_calls:
            retry_after = await run_in_threadpool(
                consume_budgets, user_id, [TOOL_CALLS_PER_MIN] * summary.tool_calls)
            if retry_after is not None:
                log_debug("MCP request refused: tool-call budget", user_id=user_id)
                return _rate_limited("rate_limited", retry_after)
    except LimitsUnavailable:
        log_debug("MCP request refused: endpoint unavailable", cause="limits_unreadable")
        return _unavailable()
    return None


def _replay_receive(body: bytes, receive: Receive) -> Receive:
    """A receive channel that hands the SDK the body the gate already read, then the real one."""
    replayed = False

    async def _receive() -> Message:
        nonlocal replayed
        if not replayed:
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return _receive


class McpGate:
    """Pure ASGI endpoint mounted at exactly `/api/mcp`, one instance per request.

    The shape of Starlette's own `HTTPEndpoint`: the route holds the CLASS, Starlette calls
    `McpGate(scope, receive, send)` and awaits the result. Holding the class rather than an instance
    keeps `route.endpoint` something `inspect.getsource` can read, which the route-table guards
    (`test_api_route_identity.py` and its siblings) do for every route. `mcp_state` is read per
    request, never at import.

    This is the ONE public `/api` path that resolves a session, and it admits only the two MCP
    scopes (`docs/identity-and-sessions.md`, the MCP carve-out).
    """

    def __init__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Hold one request's ASGI channels until the instance is awaited.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        self.scope = scope
        self.receive = receive
        self.send = send

    def __await__(self) -> Generator[Any, None, None]:
        """Run `dispatch()` when Starlette awaits the instance."""
        return self.dispatch().__await__()

    async def dispatch(self) -> None:
        """Run the checks in order, answer the first refusal as JSON, else hand to the SDK.

        Every path SENDS a response, the disconnect included. Returning without one makes
        `BaseHTTPMiddleware` raise "No response returned.", and `observability_middleware` would
        file that as an `$exception` any unauthenticated caller could trigger at will.
        """
        scope, receive, send = self.scope, self.receive, self.send
        state = mcp_state
        headers = Headers(scope=scope)
        response = (_local_kill_refusal(state) or _http_refusal(scope, headers, state)
                    or await _redis_kill_refusal())
        body = b""
        if response is None:
            try:
                body = await _read_body(receive)
            except _BodyTooLarge:
                log_debug("MCP request refused: body too large", declared=False)
                response = _refusal(413, "request_too_large", "The request body is over 256 KB.")
            except _ClientGone:
                # Nobody will read this; it exists so the middleware stack sees a response.
                log_debug("MCP request abandoned: client disconnected mid-body")
                response = _refusal(400, "client_disconnected",
                                    "The client disconnected before the request body arrived.")
        if response is None:
            response = await _query_token_refusal(scope)
        token: Optional[str] = None
        if response is None:
            token, response = _bearer_refusal(headers)
        summary = _rpc_summary(body)
        principal: Optional[McpPrincipal] = None
        if response is None and token is not None:
            principal, response = await _authenticate(scope, headers, token, summary)
        if response is not None or principal is None or token is None:
            response = response or _unavailable()
            _report_refusal(response)
            await response(scope, receive, send)
            return

        # From the first slot taken to the last byte sent, ONE try/finally: every exit path,
        # cancellation and any exception included, gives back exactly what was taken.
        admission = _Admission(principal)
        try:
            response = await _take_slots(admission, summary)
            if response is not None:
                _report_refusal(response)
                await response(scope, receive, send)
                return
            scope.setdefault("state", {})["mcp_principal"] = principal
            await run_in_threadpool(_record_access, principal, token, summary)
            await mcp_server.session_manager.handle_request(scope, _replay_receive(body, receive),
                                                            send)
        finally:
            await _release(admission)
