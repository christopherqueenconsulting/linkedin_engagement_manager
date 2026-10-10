"""Whether the automation browser's egress route can reach LinkedIn right now (#2346).

Every Selenium session is routed through an egress proxy (`utilities/proxy.py`). When that proxy
stops answering, every LinkedIn lane fails at login and backs off quietly. On 2026-10-09 that ran
for more than nine hours, and `/health/deep` would have read `healthy` throughout, because it
measured only Celery. This module is the missing reading: for each distinct egress the active
users are routed through, ask the proxy to open a tunnel to LinkedIn and report what it said.

The probe is a bare HTTP ``CONNECT www.linkedin.com:443`` — the same request Chrome makes first for
every HTTPS page behind an HTTP proxy. On a ``200`` the proxy has opened a TCP connection to
www.linkedin.com:443 from the egress IP; the probe closes it straight away, before any TLS, so no
HTTP request, cookie or account is ever sent and the bytes are negligible on a metered plan. That
happens at most once per ``EGRESS_PROBE_CACHE_SECONDS`` per API process per distinct proxy. SOCKS
proxies get a TCP connect to the proxy itself only (reachable, not proven end to end).

Results are cached in-process because the caller is an unauthenticated endpoint: a burst of
requests must not become a burst of outbound connections. Nothing here ever raises, and nothing it
returns names a host, a port or a credential.
"""

import base64
import os
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import unquote, urlparse

from cqc_lem.utilities.logger import log_debug

PROBE_TARGET_HOST = "www.linkedin.com"
PROBE_TARGET_PORT = 443

# Probe outcomes, worst first — `summarize` reports the worst one seen.
EGRESS_UNREACHABLE = "unreachable"   # no TCP connection to the proxy, or it hung up / timed out
EGRESS_AUTH_FAILED = "auth_failed"   # the proxy answered 407: credentials (or IP allowlist) refused
EGRESS_REFUSED = "refused"           # the proxy answered, but would not open the tunnel (non-200)
EGRESS_UNKNOWN = "unknown"           # we could not measure (config unreadable, probe crashed)
EGRESS_OK = "ok"                     # tunnel to LinkedIn opened (or, for SOCKS, the proxy accepted TCP)
EGRESS_DIRECT = "direct"             # no proxy configured for any active user — nothing to probe

_SEVERITY = (EGRESS_UNREACHABLE, EGRESS_AUTH_FAILED, EGRESS_REFUSED, EGRESS_UNKNOWN, EGRESS_OK)
FAILING = frozenset({EGRESS_UNREACHABLE, EGRESS_AUTH_FAILED, EGRESS_REFUSED})

_DEFAULT_TIMEOUT_SECONDS = 5.0
_DEFAULT_CACHE_SECONDS = 60
_MAX_PROBES = 10  # bound the work one /health/deep call can cause, however many proxies exist

_CACHE: dict = {"at": 0.0, "value": None, "failing_since": None}
# Shared by every API worker, so the debounce is one clock for the whole deployment rather than one
# per uvicorn process (prod runs several). Falls back to the in-process value without Redis.
_FAILING_SINCE_KEY = "health:egress_failing_since"
_FAILING_SINCE_TTL_SECONDS = 24 * 3600
_DEFAULT_DEGRADE_AFTER_SECONDS = 300
_CACHE_LOCK = threading.Lock()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _proxy_authorization(username: Optional[str], password: Optional[str]) -> Optional[str]:
    if not username:
        return None
    raw = f"{unquote(username)}:{unquote(password or '')}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def probe_proxy(proxy_url: str, timeout: Optional[float] = None) -> str:
    """Ask one egress proxy to open a tunnel to LinkedIn, and classify the answer.

    Args:
        proxy_url: ``scheme://[user:pass@]host:port`` as stored for the user or region.
        timeout: Seconds for the connect and for the proxy's reply. Defaults to
            ``EGRESS_PROBE_TIMEOUT_SECONDS`` (5).

    Returns:
        One of ``ok``, ``auth_failed``, ``refused``, ``unreachable`` or ``unknown``. Never raises.
        Under ``DEMO_MODE`` it is ``unknown`` without opening a socket: the probe asks the proxy
        for a tunnel to LinkedIn, and demo mode allows no LinkedIn connection at all (#2372).
    """
    from cqc_lem.utilities.demo_mode import is_demo_mode
    if is_demo_mode():
        return EGRESS_UNKNOWN
    timeout = timeout if timeout is not None else _env_float("EGRESS_PROBE_TIMEOUT_SECONDS",
                                                            _DEFAULT_TIMEOUT_SECONDS)
    try:
        parsed = urlparse(proxy_url)
        scheme = (parsed.scheme or "http").lower()
        host = parsed.hostname
        if not host:
            return EGRESS_UNKNOWN
        port = parsed.port or (443 if scheme == "https" else 1080 if scheme.startswith("socks")
                               else 80)
    except Exception:
        return EGRESS_UNKNOWN

    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return EGRESS_UNREACHABLE

    try:
        sock.settimeout(timeout)
        if scheme.startswith("socks"):
            # A SOCKS handshake is out of scope for a health probe; the TCP accept is the reading.
            return EGRESS_OK
        if scheme == "https":
            context = ssl.create_default_context()
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            sock = context.wrap_socket(sock, server_hostname=host)
        lines = [f"CONNECT {PROBE_TARGET_HOST}:{PROBE_TARGET_PORT} HTTP/1.1",
                 f"Host: {PROBE_TARGET_HOST}:{PROBE_TARGET_PORT}"]
        auth = _proxy_authorization(parsed.username, parsed.password)
        if auth:
            lines.append(f"Proxy-Authorization: {auth}")
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        status_line = b""
        deadline = time.monotonic() + timeout
        while b"\r\n" not in status_line and len(status_line) < 1024:
            if time.monotonic() > deadline:
                return EGRESS_UNREACHABLE  # a proxy that trickles bytes is not an answer
            chunk = sock.recv(256)
            if not chunk:
                break
            status_line += chunk
        parts = status_line.split(b"\r\n", 1)[0].split()
        if len(parts) < 2 or not parts[0].startswith(b"HTTP/"):
            return EGRESS_UNREACHABLE  # hung up or spoke something that is not HTTP
        code = parts[1]
        if code == b"200":
            return EGRESS_OK
        if code == b"407":
            return EGRESS_AUTH_FAILED
        return EGRESS_REFUSED
    except (socket.timeout, TimeoutError, ConnectionError, ssl.SSLError):
        return EGRESS_UNREACHABLE
    except Exception as e:
        log_debug(f"Egress probe could not complete: {type(e).__name__}")
        return EGRESS_UNKNOWN
    finally:
        try:
            sock.close()
        except Exception:
            pass  # Closing a probe socket that is already dead is not news.


def configured_egress_proxies() -> Optional[list]:
    """The distinct egress proxy URLs the active users' browsers are routed through.

    Resolved with the same rule `get_driver_wait_pair` uses (`proxy.resolve_proxy`: the user's own
    proxy, else their region's, else the global default). Returns an empty list when no active
    user is proxied, and None only when resolving raised. Note the DB helpers swallow a MySQL
    error and return no rows, so a database outage reads as an empty list (`direct`), not None.
    """
    try:
        from cqc_lem.utilities.db import get_active_user_ids, get_users_proxy_config
        from cqc_lem.utilities.proxy import resolve_proxy
        user_ids = get_active_user_ids() or []
        seen: list = []
        for row in get_users_proxy_config(list(user_ids)) if user_ids else []:
            url = resolve_proxy(row.get("proxy_url"), row.get("country"))
            if url and url not in seen:
                seen.append(url)
        return seen
    except Exception as e:
        log_debug(f"Egress probe could not read the proxy configuration: {type(e).__name__}")
        return None


def summarize(results: list) -> str:
    """The single `egress` reading for a list of per-proxy outcomes: the worst one, or `direct`."""
    if not results:
        return EGRESS_DIRECT
    for state in _SEVERITY:
        if state in results:
            return state
    return EGRESS_UNKNOWN


def egress_health(use_cache: bool = True) -> dict:
    """``{"egress": <state>, "egress_checked": n, "egress_failing": k}`` — counts only, no hosts.

    Args:
        use_cache: Serve a reading younger than ``EGRESS_PROBE_CACHE_SECONDS`` instead of probing.

    Returns:
        ``egress`` is the worst outcome across the distinct proxies (see `summarize`),
        ``egress_checked`` how many were probed, ``egress_failing`` how many are in a failing
        state. An unreadable configuration is ``unknown`` with both counts 0.
    """
    ttl = _env_float("EGRESS_PROBE_CACHE_SECONDS", _DEFAULT_CACHE_SECONDS)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE["value"]
        if use_cache and cached is not None and now - _CACHE["at"] < ttl:
            return dict(cached)
        proxies = configured_egress_proxies()
        if proxies is None:
            value = {"egress": EGRESS_UNKNOWN, "egress_checked": 0, "egress_failing": 0}
        else:
            targets = proxies[:_MAX_PROBES]
            if targets:
                # In parallel, so a dead proxy costs the endpoint one timeout, not one per proxy.
                with ThreadPoolExecutor(max_workers=len(targets)) as pool:
                    results = list(pool.map(probe_proxy, targets))
            else:
                results = []
            value = {"egress": summarize(results), "egress_checked": len(results),
                     "egress_failing": sum(1 for r in results if r in FAILING)}
        _note_failing(value["egress"] in FAILING)
        _CACHE.update(at=now, value=value)
        return dict(value)


def _redis():
    try:
        from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client
        return shared_redis_client()
    except Exception:
        return None


def _note_failing(failing: bool) -> None:
    """Remember when the egress started failing (first fresh failing reading), or forget it."""
    client = _redis()
    if failing:
        if _CACHE["failing_since"] is None:
            _CACHE["failing_since"] = time.time()
        if client is not None:
            try:
                client.set(_FAILING_SINCE_KEY, int(time.time()), nx=True,
                           ex=_FAILING_SINCE_TTL_SECONDS)
            except Exception:
                pass  # Fail open: the in-process clock above still counts.
    else:
        _CACHE["failing_since"] = None
        if client is not None:
            try:
                client.delete(_FAILING_SINCE_KEY)
            except Exception:
                pass  # Fail open: the key expires on its own TTL.


def failing_for_seconds() -> float:
    """How long the egress has been failing, across every API worker; 0 when it is not failing.

    Read from Redis when it answers (one clock for the deployment), else from this process's own
    first failing reading.
    """
    with _CACHE_LOCK:
        local = _CACHE["failing_since"]
    since = None
    client = _redis()
    if client is not None:
        try:
            raw = client.get(_FAILING_SINCE_KEY)
            if raw is not None:
                since = float(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception:
            since = None
    if since is None:
        since = local
    return max(0.0, time.time() - since) if since is not None else 0.0


def should_degrade() -> bool:
    """Whether the egress reading should turn a healthy `/health/deep` into `degraded`.

    Only once the egress has been failing for ``EGRESS_DEGRADE_AFTER_SECONDS`` (default 300), so a
    single dropped connection does not page anyone. ``HEALTH_DEEP_EGRESS_DEGRADES=false`` turns
    degrading off while keeping the fields.
    """
    if os.getenv("HEALTH_DEEP_EGRESS_DEGRADES", "true").strip().lower() in ("0", "false", "no",
                                                                            "off"):
        return False
    with _CACHE_LOCK:
        value = _CACHE["value"]
    if not value or value.get("egress") not in FAILING:
        return False
    return failing_for_seconds() >= _env_float("EGRESS_DEGRADE_AFTER_SECONDS",
                                               _DEFAULT_DEGRADE_AFTER_SECONDS)


def reset_cache() -> None:
    """Forget the cached reading (tests, and an operator who just fixed the proxy)."""
    with _CACHE_LOCK:
        _CACHE.update(at=0.0, value=None, failing_since=None)
