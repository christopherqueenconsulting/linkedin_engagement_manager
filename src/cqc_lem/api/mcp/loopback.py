"""The in-process loopback: how an MCP tool calls LEM's real routes (`docs/agent-mcp-plan.md` §2, §3.4).

A tool never calls a service function. It sends an ordinary HTTP request to the same FastAPI app
through `httpx2.ASGITransport`, so every guard a browser request meets runs unchanged: the scope
surface and its verb limits, ownership, the CSRF header rule, the observer field cuts and the
`api_call` event for the real route. Four headers make it an MCP request:

- `Cookie: lem_session=<token>`: the caller's MCP token, presented the way the SPA presents its own.
- `X-LEM-Client: mcp`: the client header every cookie-authenticated write needs (#957).
- `X-LEM-MCP-Dispatch: <this worker's secret>`: the audience binding `_scope_checked` demands of an
  mcp-scoped session (`dispatch.py`). It never leaves the process: the transport is an
  `ASGITransport`, never a network one, and a unit test pins that.
- `CF-Connecting-IP`: the OUTER caller's address, so the inner request is attributed to them.

The query always carries `session_token=cookie` (the non-secret `COOKIE_SESSION_SENTINEL`), never
the token, so the token appears in no URL and therefore in no access log.

Each inner call spends the caller's READ budgets (240 a minute, 4000 a day, owner ruling O10), and
runs in its own task, so a ContextVar a route sets (the session scope, the request path) cannot leak
into the next call or back into the tool.
"""

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping, Optional

import anyio
import httpx2
from fastapi.concurrency import run_in_threadpool
from starlette.requests import Request

from cqc_lem.api.mcp.dispatch import DISPATCH_HEADER, dispatch_secret
from cqc_lem.api.mcp.lifespan import mcp_state
from cqc_lem.api.mcp.limits import READ_BUDGETS, LimitsUnavailable, consume_budgets
from cqc_lem.utilities.env_constants import SESSION_COOKIE_NAME

if TYPE_CHECKING:  # the gate imports the server, which imports the tools, which import this
    from cqc_lem.api.mcp.gate import McpPrincipal

CLIENT_HEADER_VALUE = "mcp"
# Plan §2: a fan-out tool runs at most this many inner calls at once, so one tool call cannot take
# more than four of the thread pool the SPA shares.
FAN_OUT_CONCURRENCY = 4

# Outcomes (`mcp_tool_call.outcome`, plan §7).
OK = "ok"
REFUSED = "refused"
INVALID_ARGS = "invalid_args"
UPSTREAM_ERROR = "upstream_error"
RATE_LIMITED = "rate_limited"
TIMEOUT = "timeout"
UNKNOWN = "unknown"


class ToolFailure(Exception):
    """A tool call that ends as `isError`: the outcome, a stable code and a message safe to show.

    The message is LEM's own wording, never request or response content, so it may go to the agent.
    """

    def __init__(self, outcome: str, code: str, message: str,
                 inner_status: Optional[int] = None) -> None:
        """Record one failed tool call.

        Args:
            outcome: One of the outcome constants above.
            code: A stable machine-readable code (the route's `code` when it gave one).
            message: A short human-readable reason.
            inner_status: The inner route's HTTP status, when an inner call answered.
        """
        super().__init__(message)
        self.outcome = outcome
        self.code = code
        self.message = message
        self.inner_status = inner_status


def _refusal_from(status: int, body: Any) -> ToolFailure:
    """Map an inner non-200 answer onto a `ToolFailure`.

    A 422 never passes its body through: FastAPI's validation detail echoes the input values.
    """
    detail = body.get("detail") if isinstance(body, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    message = (detail.get("message") if isinstance(detail, dict) else detail)
    if not isinstance(message, str) or len(message) > 200:
        message = None
    if status == 422:
        return ToolFailure(INVALID_ARGS, "invalid_arguments",
                           "LEM rejected the arguments for this call.", status)
    if status == 429:
        return ToolFailure(RATE_LIMITED, code or "rate_limited",
                           message or "LEM is rate-limiting this account. Retry later.", status)
    if status >= 500:
        return ToolFailure(UPSTREAM_ERROR, code or ("unavailable" if status == 503 else "upstream_error"),
                           "LEM could not answer this right now. Try again shortly.", status)
    if status == 404:
        return ToolFailure(REFUSED, code or "not_found", message or "Not found.", status)
    if status in (401, 403):
        return ToolFailure(REFUSED, code or "forbidden",
                           message or "This token is not allowed to read that.", status)
    return ToolFailure(REFUSED, code or f"http_{status}", message or "LEM refused the request.",
                       status)


_ROUTE_PATHS: dict[tuple[str, str], str] = {}


def route_path(path: str, method: str = "GET") -> str:
    """The real `/api` path of the route a registry path names, for one verb.

    The registry speaks scope keys (`/posts`, no trailing slash) because that is what the grants
    are keyed on, but several routes are declared WITH one (`/api/posts/`), and asking for the other
    spelling is answered with a 307 the loopback deliberately does not follow. So the route table
    is read once and each key mapped to the path the route really has. A concrete templated path
    (`/posts/5`) has no key of its own and is sent as given. Keyed by verb too, because a write
    route and a read route can share a key with different spellings (`POST /schedule_post/`).
    """
    if not _ROUTE_PATHS:
        from cqc_lem.api import main  # lazy: main imports this package

        for route in main._walk_routes(main.app.routes):
            full = getattr(route, "path", None)
            if not full or not full.startswith("/api/"):
                continue
            for verb in getattr(route, "methods", None) or ():
                _ROUTE_PATHS.setdefault((verb, main._scope_path(full)), full)
    return _ROUTE_PATHS.get((method.upper(), path), f"/api{path}")


def build_transport() -> httpx2.ASGITransport:
    """The in-process transport to LEM's own app. Never a network transport.

    `raise_app_exceptions=False`, so a route that raises comes back as the 500 a browser would get,
    which maps to `upstream_error`, instead of an exception unwinding through the tool.
    """
    from cqc_lem.api.main import app  # lazy: main imports this package

    return httpx2.ASGITransport(app=app, raise_app_exceptions=False)


@dataclass
class Loopback:
    """One tool call's view of LEM: who is calling, and the inner calls made on their behalf.

    The token is held only to build the inner cookie and is excluded from `repr`, so it cannot reach a
    log line or a traceback through this object.
    """

    principal: "McpPrincipal"
    token: str = field(repr=False)
    client_ip: Optional[str] = None
    inner_calls: int = 0
    # Writes SENT to a route (W2): a write tool's failure before the first one changed nothing.
    writes_attempted: int = 0
    last_status: Optional[int] = None
    _limiter: anyio.Semaphore = field(default_factory=lambda: anyio.Semaphore(FAN_OUT_CONCURRENCY),
                                      repr=False)
    _client: Optional[httpx2.AsyncClient] = field(default=None, repr=False)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Cookie": f"{SESSION_COOKIE_NAME}={self.token}",
            "X-LEM-Client": CLIENT_HEADER_VALUE,
            DISPATCH_HEADER: dispatch_secret(),
            "Accept": "application/json",
        }
        if self.client_ip:
            headers["CF-Connecting-IP"] = self.client_ip
        return headers

    def _http(self) -> httpx2.AsyncClient:
        if self._client is None:
            self._client = httpx2.AsyncClient(transport=build_transport(),
                                              base_url=mcp_state.base_url or "http://localhost",
                                              headers=self._headers(), follow_redirects=False,
                                              timeout=None)
        return self._client

    async def aclose(self) -> None:
        """Close the inner client, if one was opened."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _spend_read_budget(self) -> None:
        try:
            retry_after = await run_in_threadpool(consume_budgets, self.principal.user_id,
                                                  READ_BUDGETS)
        except LimitsUnavailable as exc:
            raise ToolFailure(UPSTREAM_ERROR, "mcp_unavailable",
                              "The LEM MCP server is unavailable right now. Try again shortly.") from exc
        if retry_after is not None:
            raise ToolFailure(RATE_LIMITED, "read_budget_exhausted",
                              f"This account's MCP read budget is spent. Retry in {retry_after}s.")

    async def get(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        """GET one LEM route as the caller and return its `detail`.

        Args:
            path: The route path under `/api`, e.g. `/user/settings` (the tool registry's literal).
            params: Query parameters. `session_token` is always set to the cookie sentinel and any
                caller-supplied value for it is replaced.

        Returns:
            The route's `detail` on a 200.

        Raises:
            ToolFailure: The budget is spent, or the route answered anything but 200.
        """
        async with self._limiter:
            await self._spend_read_budget()
            from cqc_lem.api.main import COOKIE_SESSION_SENTINEL  # lazy: main imports this package

            query = {k: v for k, v in (params or {}).items() if v is not None}
            query["session_token"] = COOKIE_SESSION_SENTINEL
            self.inner_calls += 1
            client = self._http()
            # Its own task: the route's ContextVars (session scope, path, request) live and die there.
            task = asyncio.create_task(client.get(route_path(path), params=query))
            response = await task
        return self._detail(response)

    def _detail(self, response: httpx2.Response) -> Any:
        self.last_status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code != 200:
            raise _refusal_from(response.status_code, body)
        return body.get("detail") if isinstance(body, dict) else None

    async def write(self, method: str, path: str, body: Mapping[str, Any],
                    params: Optional[Mapping[str, Any]] = None,
                    keep_none: frozenset[str] = frozenset()) -> dict[str, Any]:
        """Send one write (POST or PUT) to a LEM route as the caller and return its whole body.

        A write spends no READ budget: the write tool already spent the per-user WRITE budgets once
        for the tool call (`writes.py`). The body carries `session_token=cookie`, the same sentinel a
        read puts in its query, so the real token is in no URL and no body.

        Args:
            method: `POST` or `PUT`.
            path: The registry's path, e.g. `/schedule_dm`.
            body: The JSON body; `session_token` is always replaced with the sentinel.
            params: Query parameters (`/update_post/` takes its `post_id` there).
            keep_none: Body keys whose None is sent as an explicit `null` instead of being dropped
                (restoring a stored NULL); every other None key is omitted.

        Returns:
            The route's JSON body on a 200 (`detail` plus any top-level fields, e.g. `post_id`).

        Raises:
            ToolFailure: The route answered anything but 200.
        """
        from cqc_lem.api.main import COOKIE_SESSION_SENTINEL  # lazy: main imports this package

        payload = {k: v for k, v in body.items() if v is not None or k in keep_none}
        payload["session_token"] = COOKIE_SESSION_SENTINEL
        query = {k: v for k, v in (params or {}).items() if v is not None}
        query["session_token"] = COOKIE_SESSION_SENTINEL
        async with self._limiter:
            self.inner_calls += 1
            self.writes_attempted += 1
            client = self._http()
            task = asyncio.create_task(client.request(method.upper(), route_path(path, method),
                                                      params=query, json=payload))
            response = await task
        self._detail(response)
        data = response.json()
        return data if isinstance(data, dict) else {"detail": data}


def loopback_for(request: Optional[Request]) -> Loopback:
    """The `Loopback` for the MCP request the SDK is serving, built from what the gate admitted.

    The gate put the principal in the request scope. The token is re-read from the request's own
    `Authorization` header, which the gate has already resolved to that principal; it is never
    stored anywhere else.

    Args:
        request: The outer `POST /api/mcp` request, from the SDK's request context.

    Returns:
        A loopback bound to that caller.

    Raises:
        ToolFailure: There is no admitted MCP principal on the request (never true behind the gate).
    """
    from cqc_lem.api.main import _bearer_token, _client_ip  # lazy: main imports this package

    state = getattr(request, "scope", {}).get("state") or {} if request is not None else {}
    principal = state.get("mcp_principal")
    token = _bearer_token(request.headers.get("authorization")) if request is not None else None
    if getattr(principal, "user_id", None) is None or not token:
        raise ToolFailure(REFUSED, "not_authenticated", "This MCP request is not authenticated.")
    return Loopback(principal=principal, token=token, client_ip=_client_ip(request))
