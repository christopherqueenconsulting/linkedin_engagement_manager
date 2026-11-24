"""The fail-OPEN lifespan that starts the MCP SDK's session manager (`docs/agent-mcp-plan.md` §2).

Three SDK 2.3.0 facts shape it. `StreamableHTTPSessionManager.run()` may be entered only ONCE per
instance; `MCPServer.session_manager` raises until `streamable_http_app()` has built one; and the
transport settings are arguments to that call. So every app startup calls `streamable_http_app()`
first, which builds a FRESH manager, and only then enters `run()`. That is what lets the
function-scoped `api_client` fixture start the app twice in one process.

Fail-open means the APP keeps starting. Any failure here is logged once, marks the endpoint
unavailable and still yields, so the gate answers 503 `mcp_unavailable` while `/health` and the
rest of `/api` serve normally. An empty `PUBLIC_BASE_URL` is the same outcome: without it there
is no Host or Origin to check a request against, so the gate refuses rather than guessing.
"""

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlparse

from mcp.server.transport_security import TransportSecuritySettings

from cqc_lem.api.mcp.server import mcp_server
from cqc_lem.api.spa_assets import public_base_url
from cqc_lem.utilities.flags import MCP_SERVER, env_default
from cqc_lem.utilities.logger import log_error, log_info

MCP_PATH = "/api/mcp"
MAX_REQUEST_BODY_BYTES = 256 * 1024

# A base URL on one of these hosts is a developer's own machine, the only case where the gate also
# accepts `localhost:*` / `127.0.0.1:*`. Keyed off the configured URL rather than a separate "dev"
# switch, so a production deploy cannot turn the loopback allowance on by forgetting a variable.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})

REASON_BASE_URL_EMPTY = "public_base_url_empty"
REASON_BASE_URL_INVALID = "public_base_url_invalid"
REASON_STARTUP_FAILED = "session_manager_failed"
REASON_NOT_STARTED = "not_started"


@dataclass
class McpState:
    """What the lifespan learned at startup, read by the gate on every request.

    Starts unavailable, so a gate reached without a lifespan (a client that never ran startup)
    refuses instead of serving.
    """

    unavailable: bool = True
    reason: str = REASON_NOT_STARTED
    base_url: str = ""
    host: str = ""
    origin: str = ""
    dev_loopback: bool = False
    logged_reasons: set[str] = field(default_factory=set)

    def mark_unavailable(self, reason: str) -> None:
        """Record why the endpoint cannot serve. The gate answers 503 until the next startup."""
        self.unavailable = True
        self.reason = reason


mcp_state = McpState()


def _log_unavailable_once(reason: str, message: str, exc: Optional[BaseException] = None,
                          *, always_error: bool = False) -> None:
    """Log a reason the endpoint cannot serve, the first time it is seen in this process.

    Once per process, not per startup: the unit lane starts the app thousands of times, and in
    production each worker starts once, so this is still "one startup line" there.

    A missing or unusable `PUBLIC_BASE_URL` is a defect only on a deploy that MEANT to serve MCP,
    so it is ERROR only when `mcp-server-enabled` reads on from its env fallback, and INFO
    otherwise. The env value, not a PostHog lookup: there is no user at startup, the rollout is per
    user, and a definition fetch here could stall the worker's boot. A session manager that fails
    to start (`always_error`) is a code or SDK fault and is ERROR whatever the flag says.
    """
    if reason in mcp_state.logged_reasons:
        return
    mcp_state.logged_reasons.add(reason)
    if always_error or env_default(MCP_SERVER):
        log_error(message, exc=exc, reason=reason)
    else:
        log_info(message, reason=reason)


def _split_base_url(base_url: str) -> Optional[tuple[str, str, bool]]:
    """Return `(host, origin, dev_loopback)` for a base URL, or None when it is not usable.

    `host` is the `Host` header value a request must carry: the netloc, lower-cased, with a
    default port dropped because `proxy_set_header Host $host` forwards the bare hostname.
    Malformed input is None, never a raise: `urlparse` raises `ValueError` on a broken IPv6
    literal (`https://x]`, `http://[::1`) and `.port` on a non-numeric port, and this runs at app
    startup, where a raise would stop the whole API, not just this endpoint.
    """
    try:
        parsed = urlparse(base_url)
        hostname = parsed.hostname
        parsed.port  # noqa: B018 — read only for the ValueError a malformed port raises.
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not hostname:
        return None
    netloc = parsed.netloc.lower().rsplit("@", 1)[-1]
    default_port = ":443" if parsed.scheme == "https" else ":80"
    if netloc.endswith(default_port):
        netloc = netloc[: -len(default_port)]
    origin = f"{parsed.scheme}://{netloc}"
    return netloc, origin, hostname.lower() in LOOPBACK_HOSTS


def _transport_security(host: str, origin: str, dev_loopback: bool) -> TransportSecuritySettings:
    """The SDK's own Host/Origin check, set to agree with the gate's.

    The gate checks first and answers in JSON; this is the second wall, so the SDK never runs
    with its permissive default if a request ever reaches it another way.
    """
    hosts = [host]
    origins = [origin]
    if dev_loopback:
        # The same three loopback names the gate accepts, so the two walls never disagree.
        hosts += ["localhost", "127.0.0.1", "[::1]", "localhost:*", "127.0.0.1:*", "[::1]:*"]
        origins += ["http://localhost", "http://127.0.0.1", "http://[::1]", "http://localhost:*",
                    "http://127.0.0.1:*", "http://[::1]:*"]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=hosts, allowed_origins=origins)


@asynccontextmanager
async def mcp_lifespan(app: Any) -> AsyncIterator[None]:
    """Start the MCP session manager for one app lifetime, never failing the app's startup.

    Args:
        app: The FastAPI app being started. Unused; the signature is the lifespan protocol's.

    Yields:
        Control to the running app, always, whether or not the MCP stack started.
    """
    base_url = public_base_url()
    mcp_state.mark_unavailable(REASON_NOT_STARTED)
    mcp_state.base_url = base_url
    mcp_state.host, mcp_state.origin, mcp_state.dev_loopback = "", "", False
    split = _split_base_url(base_url) if base_url else None
    if not base_url:
        _log_unavailable_once(REASON_BASE_URL_EMPTY,
                              "MCP endpoint unavailable: PUBLIC_BASE_URL is empty")
        mcp_state.mark_unavailable(REASON_BASE_URL_EMPTY)
    elif split is None:
        _log_unavailable_once(REASON_BASE_URL_INVALID,
                              "MCP endpoint unavailable: PUBLIC_BASE_URL is not a usable "
                              "http(s) URL")
        mcp_state.mark_unavailable(REASON_BASE_URL_INVALID)

    stack = AsyncExitStack()
    if split is not None:
        host, origin, dev_loopback = split
        mcp_state.host, mcp_state.origin, mcp_state.dev_loopback = host, origin, dev_loopback
        try:
            mcp_server.streamable_http_app(
                streamable_http_path=MCP_PATH,
                stateless_http=True,
                json_response=True,
                max_request_body_size=MAX_REQUEST_BODY_BYTES,
                transport_security=_transport_security(host, origin, dev_loopback),
            )
            await stack.enter_async_context(mcp_server.session_manager.run())
        except Exception as exc:  # noqa: BLE001 — fail open: the app must start regardless.
            _log_unavailable_once(REASON_STARTUP_FAILED,
                                  "MCP endpoint unavailable: session manager failed to start",
                                  exc=exc, always_error=True)
            mcp_state.mark_unavailable(REASON_STARTUP_FAILED)
        else:
            mcp_state.unavailable = False
            mcp_state.reason = ""
            log_info("MCP session manager started", path=MCP_PATH, dev_loopback=dev_loopback)
    try:
        yield
    finally:
        mcp_state.mark_unavailable(REASON_NOT_STARTED)
        try:
            await stack.aclose()
        except Exception as exc:  # noqa: BLE001 — shutdown must not mask the app's own exit.
            log_error("MCP session manager failed to shut down cleanly", exc=exc)
