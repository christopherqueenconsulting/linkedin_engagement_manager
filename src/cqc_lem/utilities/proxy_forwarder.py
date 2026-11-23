"""Authenticating egress forwarder: Chrome connects with NO credentials, this adds them upstream (#2620).

Why it exists. Chrome's ``--proxy-server`` cannot carry ``user:pass``, so a credentialed proxy used
to be answered by an in-memory auth extension (`selenium_util._build_proxy_auth_extension_b64`).
Current Chrome ignores ``--load-extension`` and the encoded-extension path with it, so the extension
never loaded and every session quietly relied on the provider's IP allowlist. On 2026-11-23 the
provider moved the proxy to a new IP, the allowlist moved with it, and every browser session got a
407 for about 10 hours. This process removes the extension from the path entirely.

How it works. One listener per user, on ``PROXY_FORWARDER_BASE_PORT + user_id``, inside the compose
network only (the service publishes no port). Chrome for user N is started with
``--proxy-server=http://<PROXY_FORWARDER_HOST>:<base+N>``. The listener's port IS the identity: a
connection on port base+N is only ever forwarded to user N's own upstream, resolved with the same
rule every session uses (`proxy.resolve_proxy`: the user's own proxy, else their region's, else the
global default). The upstream credentials are added here as ``Proxy-Authorization`` and never leave
this process: Chrome, the Grid and the logs see none of them.

Fail closed. Nothing here ever connects to a destination directly. No route for a port, an
unreachable upstream, or an upstream that refuses the tunnel is an error response to Chrome (502,
504, or the upstream's own status), never a fallback to the host's IP. A database that cannot be
read keeps the routes last read; it never clears them into direct egress.

The routes are re-read every ``PROXY_FORWARDER_REFRESH_SECONDS`` (30), so an edit to
``users.proxy_url`` takes effect within one refresh, with no restart.
"""

import asyncio
import base64
import json
import os
import re
import socket
import ssl
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import unquote, urlparse

from cqc_lem.utilities.logger import log_debug, log_info, log_warning

DEFAULT_BASE_PORT = 20000
_MAX_PORT = 65535
_HEAD_LIMIT = 64 * 1024
_COPY_CHUNK = 64 * 1024
_DEFAULT_REFRESH_SECONDS = 30.0
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
_DEFAULT_HEAD_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_LISTENERS = 1000
FORWARDABLE_SCHEMES = ("http", "https")
HEALTH_PATH = "/healthz"

# Headers that describe the CLIENT's hop to us, never the upstream hop. A client-supplied
# Proxy-Authorization is dropped so nothing on the compose network can pick another credential.
_HOP_HEADERS = frozenset({"proxy-authorization", "proxy-connection", "connection", "keep-alive",
                          "te", "trailer", "upgrade"})
# host:port, IPv4/hostname or a bracketed IPv6 literal. Anything else is refused before it is
# written into an upstream request line.
_CONNECT_TARGET = re.compile(r"^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.\-]+):\d{1,5}$")
_HTTP_VERSION = re.compile(r"^HTTP/1\.[01]$")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def forwarder_host() -> str:
    """The forwarder's compose service name, or ``""`` when the forwarder is not in use."""
    return (os.getenv("PROXY_FORWARDER_HOST") or "").strip()


def is_enabled() -> bool:
    """Whether proxied Chrome sessions go through the forwarder (``PROXY_FORWARDER_HOST`` set)."""
    return bool(forwarder_host())


def base_port() -> int:
    """The first port of the per-user range; the port itself serves only the health check."""
    port = _env_int("PROXY_FORWARDER_BASE_PORT", DEFAULT_BASE_PORT)
    return port if 1024 <= port < _MAX_PORT else DEFAULT_BASE_PORT


def port_for_user(user_id: int) -> Optional[int]:
    """The listener port for this user, or None when the id cannot be mapped into the range."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    if uid <= 0:
        return None
    port = base_port() + uid
    return port if port <= _MAX_PORT else None


def can_forward(proxy_url: Optional[str]) -> bool:
    """Whether the forwarder can carry this upstream: an HTTP or HTTPS proxy with a host.

    SOCKS is not forwarded: Chrome cannot authenticate to SOCKS either way, so such a proxy keeps
    the direct ``--proxy-server`` path it always had.
    """
    if not proxy_url:
        return False
    try:
        parsed = urlparse(proxy_url)
        return (parsed.scheme or "http").lower() in FORWARDABLE_SCHEMES and bool(parsed.hostname)
    except ValueError:
        return False


def chrome_proxy_server(user_id: int) -> Optional[str]:
    """The ``--proxy-server`` value for this user's Chrome, or None when it cannot be routed."""
    host = forwarder_host()
    port = port_for_user(user_id)
    if not host or port is None:
        return None
    return f"http://{host}:{port}"


def unroutable_proxy_server() -> str:
    """A ``--proxy-server`` value that fails every request: the health port answers proxying with 502.

    For a session whose user id cannot be mapped to a port. Pointing Chrome here keeps the session
    failing closed instead of letting it start with no proxy at all.
    """
    return f"http://{forwarder_host()}:{base_port()}"


@dataclass(frozen=True)
class Upstream:
    """One user's upstream proxy. ``authorization`` is the header value and never appears in repr."""

    scheme: str
    host: str
    port: int
    authorization: Optional[str] = field(default=None, repr=False, compare=True)

    def label(self) -> str:
        """``host:port`` for logs; never the credentials."""
        return f"{self.host}:{self.port}"


def parse_upstream(proxy_url: Optional[str]) -> Optional[Upstream]:
    """Turn ``scheme://[user:pass@]host:port`` into an `Upstream`, or None when it cannot be forwarded."""
    if not can_forward(proxy_url):
        return None
    try:
        parsed = urlparse(proxy_url)
        scheme = (parsed.scheme or "http").lower()
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    authorization = None
    if parsed.username:
        raw = f"{unquote(parsed.username)}:{unquote(parsed.password or '')}".encode("utf-8")
        authorization = "Basic " + base64.b64encode(raw).decode("ascii")
    return Upstream(scheme=scheme, host=parsed.hostname, port=port, authorization=authorization)


def load_routes_from_db() -> Optional[dict]:
    """``{user_id: Upstream}`` for every user whose session is proxied, or None when the DB is unreadable.

    Every user, not only the active ones: a first sign-in (not yet "active") opens a browser too, and
    a user without a listener would fail closed rather than egress direct, which is safe but blocks
    onboarding.
    """
    from cqc_lem.utilities.db import get_all_users_proxy_config
    from cqc_lem.utilities.proxy import resolve_proxy

    rows = get_all_users_proxy_config()
    if rows is None:
        return None
    routes: dict = {}
    for row in rows:
        upstream = parse_upstream(resolve_proxy(row.get("proxy_url"), row.get("country")))
        if upstream is not None:
            routes[int(row["user_id"])] = upstream
    return routes


def _response(code: int, reason: str, body: bytes = b"",
              content_type: str = "text/plain") -> bytes:
    head = (f"HTTP/1.1 {code} {reason}\r\nContent-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n")
    return head.encode("ascii") + body


def _parse_head(head: bytes) -> Optional[tuple]:
    """``(method, target, version, [(name, value), ...])`` from a request head, or None if malformed."""
    try:
        text = head.decode("latin-1")
    except UnicodeDecodeError:  # pragma: no cover - latin-1 decodes every byte
        return None
    lines = text.split("\r\n")
    if any("\n" in line or "\r" in line or "\x00" in line for line in lines):
        # A bare LF, CR or NUL is how a header gets smuggled into the upstream request.
        return None
    parts = lines[0].split(" ")
    if len(parts) != 3 or not _HTTP_VERSION.match(parts[2]):
        return None
    headers = []
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip():
            return None
        headers.append((name, value.strip()))
    return parts[0].upper(), parts[1], parts[2], headers


def _status_code(head: bytes) -> Optional[int]:
    parts = head.split(b"\r\n", 1)[0].split()
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/"):
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


async def _close(writer: Optional[asyncio.StreamWriter]) -> None:
    if writer is None:
        return
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass  # Closing a socket the other side already dropped is not news.


async def _copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await src.read(_COPY_CHUNK)
            if not data:
                return
            dst.write(data)
            await dst.drain()
    except (ConnectionError, OSError, asyncio.IncompleteReadError):
        return


async def _pipe(a_reader: asyncio.StreamReader, a_writer: asyncio.StreamWriter,
                b_reader: asyncio.StreamReader, b_writer: asyncio.StreamWriter) -> None:
    """Copy both ways until either side closes, then close both."""
    tasks = [asyncio.ensure_future(_copy(a_reader, b_writer)),
             asyncio.ensure_future(_copy(b_reader, a_writer))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _close(a_writer)
        await _close(b_writer)


class ProxyForwarder:
    """The per-user listeners, their routes, and the counters the health check reports.

    Args:
        load_routes: Returns ``{user_id: Upstream}``, or None when the source cannot be read (the
            last routes are kept). Called in a worker thread.
        bind_host: Interface to listen on. ``0.0.0.0`` inside the container; the compose service
            publishes no port, so only the compose network can reach it.
        base: First port of the range (``PROXY_FORWARDER_BASE_PORT``); serves the health check.
    """

    def __init__(self, load_routes: Callable[[], Optional[dict]], *, bind_host: str = "0.0.0.0",
                 base: Optional[int] = None, connect_timeout: Optional[float] = None,
                 head_timeout: Optional[float] = None, max_listeners: Optional[int] = None) -> None:
        """Start with no routes and no listeners; `refresh` loads both."""
        self._load_routes = load_routes
        self._bind_host = bind_host
        self._base = base if base is not None else base_port()
        self._connect_timeout = connect_timeout if connect_timeout is not None else _env_float(
            "PROXY_FORWARDER_CONNECT_TIMEOUT_SECONDS", _DEFAULT_CONNECT_TIMEOUT_SECONDS)
        self._head_timeout = head_timeout if head_timeout is not None else _env_float(
            "PROXY_FORWARDER_HEAD_TIMEOUT_SECONDS", _DEFAULT_HEAD_TIMEOUT_SECONDS)
        self._max_listeners = max_listeners if max_listeners is not None else _env_int(
            "PROXY_FORWARDER_MAX_LISTENERS", _DEFAULT_MAX_LISTENERS)
        self._routes: dict = {}
        self._servers: dict = {}
        self._health_server = None
        self._source_failing = False
        self._over_cap_warned = False
        self.routes_loaded_at: Optional[float] = None
        self.stats = {"tunnels_ok": 0, "plain_forwarded": 0, "no_route": 0, "bad_request": 0,
                      "upstream_unreachable": 0, "upstream_timeout": 0,
                      "upstream_auth_failed": 0, "upstream_refused": 0}

    def port_for(self, user_id: int) -> Optional[int]:
        """This forwarder's port for a user (its own base, so tests can bind an ephemeral range)."""
        port = self._base + int(user_id)
        return port if 0 < int(user_id) and port <= _MAX_PORT else None

    @property
    def listening_user_ids(self) -> list:
        """User ids with an open listener, sorted."""
        return sorted(self._servers)

    async def start_health(self) -> None:
        """Open the health listener on the base port."""
        self._health_server = await asyncio.start_server(
            self._handle_health, self._bind_host, self._base, limit=_HEAD_LIMIT)

    async def refresh(self) -> None:
        """Re-read the routes and open or close listeners to match. Never raises."""
        try:
            routes = await asyncio.to_thread(self._load_routes)
        except Exception as e:
            log_debug(f"Proxy forwarder could not load routes: {type(e).__name__}")
            routes = None
        if routes is None:
            if not self._source_failing:
                # Once per outage, not per refresh: the routes already loaded keep serving.
                log_warning("Proxy forwarder could not read the proxy routes; keeping the last "
                            f"{len(self._routes)} route(s)")
            self._source_failing = True
            return
        if self._source_failing:
            log_info("Proxy forwarder can read the proxy routes again")
        self._source_failing = False

        wanted = {uid: up for uid, up in routes.items() if self.port_for(uid) is not None}
        if len(wanted) > self._max_listeners:
            if not self._over_cap_warned:
                log_warning(f"Proxy forwarder has {len(wanted)} proxied users, over its cap of "
                            f"{self._max_listeners}; the users above the cap fail closed")
                self._over_cap_warned = True
            wanted = {uid: wanted[uid] for uid in sorted(wanted)[:self._max_listeners]}
        self._routes = wanted
        for uid in [u for u in self._servers if u not in wanted]:
            server = self._servers.pop(uid)
            server.close()
        for uid in [u for u in wanted if u not in self._servers]:
            try:
                self._servers[uid] = await asyncio.start_server(
                    lambda r, w, _uid=uid: self._handle(_uid, r, w), self._bind_host,
                    self.port_for(uid), limit=_HEAD_LIMIT)
            except OSError as e:
                log_warning(f"Proxy forwarder could not listen for user {uid}", exc=e,
                            user_id=uid)
        self.routes_loaded_at = time.time()

    async def close(self) -> None:
        """Stop every listener (open tunnels are left to finish)."""
        servers = list(self._servers.values())
        if self._health_server is not None:
            servers.append(self._health_server)
        self._servers.clear()
        self._health_server = None
        for server in servers:
            server.close()
        for server in servers:
            try:
                await server.wait_closed()
            except Exception:
                pass  # A listener that is already gone has nothing left to close.

    async def serve_forever(self, refresh_seconds: Optional[float] = None) -> None:
        """Health listener, first refresh, then refresh on a fixed period until cancelled."""
        period = refresh_seconds if refresh_seconds is not None else _env_float(
            "PROXY_FORWARDER_REFRESH_SECONDS", _DEFAULT_REFRESH_SECONDS)
        await self.start_health()
        log_info(f"Proxy forwarder listening from port {self._base}")
        try:
            while True:
                await self.refresh()
                await asyncio.sleep(max(1.0, period))
        finally:
            await self.close()

    def health(self) -> dict:
        """Counts only: listeners, route age and outcome counters. No host, port or credential."""
        age = None if self.routes_loaded_at is None else round(time.time() - self.routes_loaded_at)
        return {"status": "ok" if self.routes_loaded_at is not None else "starting",
                "listeners": len(self._servers), "routes_age_seconds": age,
                "source_failing": self._source_failing, **self.stats}

    async def _read_head(self, reader: asyncio.StreamReader) -> Optional[bytes]:
        try:
            return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self._head_timeout)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                ConnectionError, OSError):
            return None

    async def _handle_health(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        try:
            head = await self._read_head(reader)
            parsed = _parse_head(head) if head else None
            if parsed and parsed[0] == "GET" and parsed[1] == HEALTH_PATH:
                body = json.dumps(self.health()).encode("utf-8")
                code = 200 if self.routes_loaded_at is not None else 503
                writer.write(_response(code, "OK" if code == 200 else "Starting", body,
                                       "application/json"))
            else:
                # Includes every proxy request: this port belongs to no user (fail closed).
                writer.write(_response(502, "No Upstream For This Port"))
            await writer.drain()
        except (ConnectionError, OSError):
            pass  # The health client went away; nothing to answer.
        finally:
            await _close(writer)

    async def _open_upstream(self, upstream: Upstream) -> tuple:
        context = None
        if upstream.scheme == "https":
            context = ssl.create_default_context()
            context.minimum_version = ssl.TLSVersion.TLSv1_2
        return await asyncio.wait_for(
            asyncio.open_connection(upstream.host, upstream.port, ssl=context,
                                    server_hostname=upstream.host if context else None,
                                    limit=_HEAD_LIMIT),
            self._connect_timeout)

    async def _handle(self, user_id: int, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        up_writer = None
        try:
            head = await self._read_head(reader)
            parsed = _parse_head(head) if head else None
            if parsed is None:
                self.stats["bad_request"] += 1
                writer.write(_response(400, "Bad Request"))
                await writer.drain()
                return
            method, target, version, headers = parsed
            upstream = self._routes.get(user_id)
            if upstream is None:
                self.stats["no_route"] += 1
                writer.write(_response(502, "No Upstream For This User"))
                await writer.drain()
                return
            if method == "CONNECT":
                if not _CONNECT_TARGET.match(target):
                    self.stats["bad_request"] += 1
                    writer.write(_response(400, "Bad Request"))
                    await writer.drain()
                    return
                lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
            elif target.lower().startswith("http://"):
                lines = [f"{method} {target} {version}"]
                lines += [f"{name}: {value}" for name, value in headers
                          if name.lower() not in _HOP_HEADERS]
                lines.append("Connection: close")
            else:
                # Origin-form ("GET /path") is a request for US, not a proxy request.
                self.stats["bad_request"] += 1
                writer.write(_response(400, "Bad Request"))
                await writer.drain()
                return
            if upstream.authorization:
                lines.append(f"Proxy-Authorization: {upstream.authorization}")
            request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

            try:
                up_reader, up_writer = await self._open_upstream(upstream)
            except asyncio.TimeoutError:
                self.stats["upstream_timeout"] += 1
                writer.write(_response(504, "Upstream Proxy Timeout"))
                await writer.drain()
                return
            except (OSError, ssl.SSLError):
                self.stats["upstream_unreachable"] += 1
                writer.write(_response(502, "Upstream Proxy Unreachable"))
                await writer.drain()
                return
            up_writer.write(request)
            await up_writer.drain()

            if method != "CONNECT":
                self.stats["plain_forwarded"] += 1
                await _pipe(reader, writer, up_reader, up_writer)
                up_writer = None
                return

            try:
                answer = await asyncio.wait_for(up_reader.readuntil(b"\r\n\r\n"),
                                                self._head_timeout)
            except asyncio.TimeoutError:
                self.stats["upstream_timeout"] += 1
                writer.write(_response(504, "Upstream Proxy Timeout"))
                await writer.drain()
                return
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError,
                    OSError):
                self.stats["upstream_unreachable"] += 1
                writer.write(_response(502, "Upstream Proxy Unreachable"))
                await writer.drain()
                return
            code = _status_code(answer)
            if code == 200:
                self.stats["tunnels_ok"] += 1
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                await _pipe(reader, writer, up_reader, up_writer)
                up_writer = None
                return
            # Relay the status (407 stays 407, so a probe through here classifies it exactly), but
            # never the upstream's Proxy-Authenticate: Chrome must not be invited to try its own.
            if code == 407:
                self.stats["upstream_auth_failed"] += 1
                writer.write(_response(407, "Upstream Proxy Authentication Failed"))
            elif code is None:
                self.stats["upstream_unreachable"] += 1
                writer.write(_response(502, "Upstream Proxy Unreachable"))
            else:
                self.stats["upstream_refused"] += 1
                writer.write(_response(code, "Upstream Proxy Refused"))
            await writer.drain()
            log_debug(f"Proxy forwarder: upstream {upstream.label()} answered {code} for user "
                      f"{user_id}")
        except (ConnectionError, OSError):
            return  # Chrome or the upstream dropped the connection mid-exchange.
        finally:
            await _close(up_writer)
            await _close(writer)


def check(host: str = "127.0.0.1", port: Optional[int] = None, timeout: float = 5.0) -> int:
    """Container healthcheck: 0 when the health listener answers 200, else 1."""
    port = port if port is not None else base_port()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(f"GET {HEALTH_PATH} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode("ascii"))
            data = sock.recv(256)
    except OSError:
        return 1
    return 0 if _status_code(data) == 200 else 1


def main(argv: Optional[list] = None) -> int:
    """Run the forwarder (``python -m cqc_lem.utilities.proxy_forwarder``) or, with ``--check``, probe it."""
    argv = sys.argv[1:] if argv is None else argv
    if "--check" in argv:
        return check()
    forwarder = ProxyForwarder(load_routes_from_db)
    try:
        asyncio.run(forwarder.serve_forever())
    except KeyboardInterrupt:
        pass  # Ctrl-C in a foreground run is a normal stop, not an error.
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
