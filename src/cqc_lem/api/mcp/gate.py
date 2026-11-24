"""`McpGate`: the pure-ASGI front door of `POST /api/mcp` (`docs/agent-mcp-plan.md` §3.3).

Every check is the gate's own and answers JSON, in a fixed order, BEFORE the SDK sees the
request: the SDK's defaults answer plain text and are not what decides who gets in.

1. Kill switch: `MCP_SERVER_KILL` or the `mcp:kill` Redis key -> 503 `mcp_disabled_by_operator`;
   an unreadable Redis, a failed lifespan or an empty `PUBLIC_BASE_URL` -> 503 `mcp_unavailable`.
2. Method: anything but POST -> 405 with `Allow: POST`.
3. Host must be `PUBLIC_BASE_URL`'s (plus loopback on a loopback base) -> else 421; an Origin, when
   present, must be its origin -> else 403.
4. Content-Type must be `application/json` -> else 415; a body over 256 KB, declared or streamed,
   -> 413.
5. A token in the query string (`token`, `session_token`, `access_token`) -> 400
   `token_in_url_refused`. The value is never logged.
6. Only `Authorization: Bearer` is read; an outer `lem_session` cookie is ignored, and a cookie with
   no bearer -> 401 `cookie_only`.

Phase P1 stops there: no `mcp`-scoped session can exist yet, so every bearer that reaches the auth
step gets 401 `invalid_token`. Resolve, scope, rollout flag and limits are steps 7+ and land in P2.

The token is never logged, put in a response or put in exception text.
"""

from typing import Any, Awaitable, Callable, Generator, MutableMapping, Optional

from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import Headers, QueryParams
from starlette.responses import JSONResponse

from cqc_lem.api.mcp.lifespan import MAX_REQUEST_BODY_BYTES, McpState, mcp_state
from cqc_lem.api.mcp.limits import KillState, kill_env_set, read_kill_key
from cqc_lem.utilities.env_constants import SESSION_COOKIE_NAME
from cqc_lem.utilities.logger import log_debug, log_info

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


def _refusal(status_code: int, reason: str, detail: str,
             headers: Optional[dict[str, str]] = None) -> JSONResponse:
    """The gate's one response shape: `{status_code, detail, reason}`, the house refusal body."""
    return JSONResponse(status_code=status_code, headers=headers,
                        content={"status_code": status_code, "detail": detail, "reason": reason})


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
    for loopback in ("http://localhost", "http://127.0.0.1"):
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


async def _kill_switch_refusal(state: McpState) -> Optional[JSONResponse]:
    """Step 1. Env first (no I/O), then the Redis key, then whether the lifespan came up."""
    if kill_env_set():
        log_debug("MCP request refused: kill switch on", source="env")
        return _refusal(503, "mcp_disabled_by_operator",
                        "The LEM MCP server is switched off by the operator.")
    kill = await run_in_threadpool(read_kill_key)
    if kill is KillState.ON:
        log_debug("MCP request refused: kill switch on", source="redis")
        return _refusal(503, "mcp_disabled_by_operator",
                        "The LEM MCP server is switched off by the operator.")
    if kill is KillState.UNREADABLE or state.unavailable:
        log_debug("MCP request refused: endpoint unavailable",
                  cause="kill_key_unreadable" if kill is KillState.UNREADABLE else state.reason)
        return _refusal(503, "mcp_unavailable",
                        "The LEM MCP server is unavailable right now. Try again shortly.")
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


def _auth_refusal(scope: Scope, headers: Headers) -> JSONResponse:
    """Steps 5-6, and P1's end of the line: no bearer can resolve to an MCP session yet."""
    query = QueryParams(scope.get("query_string", b"").decode("latin-1"))
    if any(name in query for name in QUERY_TOKEN_PARAMS):
        # INFO, never the value. Revoking a token that resolves (and auditing it as exposed) needs
        # the resolve step, which lands in P2 with the `mcp` scopes.
        log_info("MCP request refused: token in query string",
                 params=sorted(name for name in QUERY_TOKEN_PARAMS if name in query))
        return _refusal(400, "token_in_url_refused",
                        "Never put a token in a URL. Send it as `Authorization: Bearer`, and "
                        "rotate it in LEM → Agents if it was ever shared.")
    token = _bearer(headers.get("authorization"))
    if token is None:
        cookies = [c.split("=", 1)[0].strip() for c in (headers.get("cookie") or "").split(";")]
        if SESSION_COOKIE_NAME in cookies:
            log_debug("MCP request refused: browser cookie without a bearer")
            return _refusal(401, "cookie_only", COOKIE_ONLY_DETAIL,
                            headers={"WWW-Authenticate": "Bearer"})
        log_debug("MCP request refused: no bearer token")
        return _refusal(401, "missing_token", MISSING_TOKEN_DETAIL,
                        headers={"WWW-Authenticate": "Bearer"})
    log_debug("MCP request refused: bearer does not resolve to an MCP session")
    return _refusal(401, "invalid_token", INVALID_TOKEN_DETAIL,
                    headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})


class McpGate:
    """Pure ASGI endpoint mounted at exactly `/api/mcp`, one instance per request.

    The shape of Starlette's own `HTTPEndpoint`: the route holds the CLASS, Starlette calls
    `McpGate(scope, receive, send)` and awaits the result. Holding the class rather than an instance
    keeps `route.endpoint` something `inspect.getsource` can read, which the route-table guards
    (`test_api_route_identity.py` and its siblings) do for every route. `mcp_state` is read per
    request, never at import.
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
        """Run the §3.3 checks in order and answer the first refusal as JSON."""
        scope, receive, send = self.scope, self.receive, self.send
        state = mcp_state
        headers = Headers(scope=scope)
        response = await _kill_switch_refusal(state) or _http_refusal(scope, headers, state)
        if response is None:
            try:
                await _read_body(receive)
            except _BodyTooLarge:
                log_debug("MCP request refused: body too large", declared=False)
                response = _refusal(413, "request_too_large", "The request body is over 256 KB.")
            except _ClientGone:
                return
        if response is None:
            response = _auth_refusal(scope, headers)
        await response(scope, receive, send)
