"""Unit tests for `utilities/proxy_forwarder.py` — the authenticating egress forwarder (#2620).

Everything runs over real loopback sockets: a fake upstream proxy on 127.0.0.1 records what the
forwarder sent it, and the forwarder listens on an ephemeral port range. Nothing leaves the machine.
Every credential-shaped string here (alice:s3cret-A, ...) is a fake placeholder.
"""

import asyncio
import base64
import json
import socket
from unittest.mock import patch

import pytest

from cqc_lem.utilities import proxy_forwarder as pf

pytestmark = pytest.mark.unit

_SECRET_A = "s3cret-A"
_SECRET_B = "s3cret-B"


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _free_base() -> int:
    """A base port whose next few ports were free a moment ago (good enough on loopback)."""
    for _ in range(50):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        base = s.getsockname()[1]
        s.close()
        if base + 10 > 65535:
            continue
        ok = True
        for offset in range(0, 4):
            probe = socket.socket()
            try:
                probe.bind(("127.0.0.1", base + offset))
            except OSError:
                ok = False
            finally:
                probe.close()
        if ok:
            return base
    raise RuntimeError("no free port range")


class _Upstream:
    """A fake upstream HTTP proxy: records each request head, answers with `reply`, then echoes."""

    def __init__(self, reply: bytes = b"HTTP/1.1 200 Connection established\r\n\r\n"):
        self.reply = reply
        self.heads: list = []
        self.server = None
        self.port = None

    async def start(self) -> "_Upstream":
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def _serve(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            self.heads.append(head.decode("latin-1"))
            writer.write(self.reply)
            await writer.drain()
            if self.reply.startswith(b"HTTP/1.1 200"):
                while True:
                    data = await reader.read(1024)
                    if not data:
                        break
                    writer.write(b"echo:" + data)
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    def url(self, user: str = "", password: str = "") -> str:
        cred = f"{user}:{password}@" if user else ""
        return f"http://{cred}127.0.0.1:{self.port}"


async def _forwarder(routes, base=None, **kw) -> pf.ProxyForwarder:
    base = base if base is not None else _free_base()
    fwd = pf.ProxyForwarder(lambda: routes() if callable(routes) else routes, bind_host="127.0.0.1",
                            base=base, connect_timeout=2, head_timeout=2, **kw)
    await fwd.start_health()
    await fwd.refresh()
    return fwd


async def _exchange(port: int, request: bytes, then: bytes = b"") -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    data = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
    if then:
        writer.write(then)
        await writer.drain()
        data += await asyncio.wait_for(reader.read(1024), 3)
    writer.close()
    return data


_CONNECT = b"CONNECT www.linkedin.com:443 HTTP/1.1\r\nHost: www.linkedin.com:443\r\n\r\n"


class TestConfig:
    def test_off_without_a_host(self, monkeypatch):
        monkeypatch.delenv("PROXY_FORWARDER_HOST", raising=False)
        assert pf.is_enabled() is False
        assert pf.chrome_proxy_server(3) is None

    def test_port_is_base_plus_user_id(self, monkeypatch):
        monkeypatch.setenv("PROXY_FORWARDER_HOST", "proxy-forwarder")
        monkeypatch.delenv("PROXY_FORWARDER_BASE_PORT", raising=False)
        assert pf.chrome_proxy_server(7) == "http://proxy-forwarder:20007"

    def test_a_custom_base_port(self, monkeypatch):
        monkeypatch.setenv("PROXY_FORWARDER_HOST", "proxy-forwarder")
        monkeypatch.setenv("PROXY_FORWARDER_BASE_PORT", "30000")
        assert pf.port_for_user(1) == 30001

    def test_a_bad_base_port_falls_back(self, monkeypatch):
        monkeypatch.setenv("PROXY_FORWARDER_BASE_PORT", "80")
        assert pf.base_port() == pf.DEFAULT_BASE_PORT
        monkeypatch.setenv("PROXY_FORWARDER_BASE_PORT", "nope")
        assert pf.base_port() == pf.DEFAULT_BASE_PORT

    @pytest.mark.parametrize("uid", [0, -1, 50000, "x", None])
    def test_ids_outside_the_range_have_no_port(self, uid):
        assert pf.port_for_user(uid) is None

    def test_unroutable_is_the_health_port(self, monkeypatch):
        monkeypatch.setenv("PROXY_FORWARDER_HOST", "proxy-forwarder")
        monkeypatch.delenv("PROXY_FORWARDER_BASE_PORT", raising=False)
        assert pf.unroutable_proxy_server() == "http://proxy-forwarder:20000"

    @pytest.mark.parametrize("url,ok", [("http://h:1", True), ("https://h:1", True),
                                        ("socks5://h:1", False), ("//h:1", True),
                                        ("not-a-url", False), (None, False), ("", False)])
    def test_can_forward(self, url, ok):
        assert pf.can_forward(url) is ok


class TestParseUpstream:
    def test_credentials_become_a_basic_header(self):
        up = pf.parse_upstream(f"http://alice:{_SECRET_A}@proxy.example:12323")
        assert (up.scheme, up.host, up.port) == ("http", "proxy.example", 12323)
        assert up.authorization == _basic("alice", _SECRET_A)

    def test_percent_encoded_credentials_are_decoded(self):
        up = pf.parse_upstream("http://us%40er:p%3Ass@proxy.example:1")
        assert up.authorization == _basic("us@er", "p:ss")

    def test_repr_and_label_never_carry_the_credential(self):
        up = pf.parse_upstream(f"http://alice:{_SECRET_A}@proxy.example:12323")
        assert _SECRET_A not in repr(up) and "Basic" not in repr(up)
        assert up.label() == "proxy.example:12323"

    def test_default_ports(self):
        assert pf.parse_upstream("http://h").port == 80
        assert pf.parse_upstream("https://h").port == 443

    def test_no_credentials_no_header(self):
        assert pf.parse_upstream("http://h:1").authorization is None

    def test_socks_is_not_forwarded(self):
        assert pf.parse_upstream("socks5://u:p@h:1") is None


class TestLoadRoutesFromDb:
    def test_resolves_each_user_and_skips_unforwardable(self, monkeypatch):
        monkeypatch.delenv("REGION_PROXIES", raising=False)
        monkeypatch.setenv("PROXY_URL", "http://g:3128")
        rows = [{"user_id": 1, "proxy_url": f"http://alice:{_SECRET_A}@a:1", "country": "US"},
                {"user_id": 2, "proxy_url": None, "country": None},
                {"user_id": 3, "proxy_url": "socks5://s:1", "country": None}]
        with patch("cqc_lem.utilities.db.get_all_users_proxy_config", return_value=rows):
            routes = pf.load_routes_from_db()
        assert sorted(routes) == [1, 2]
        assert routes[1].host == "a" and routes[1].authorization == _basic("alice", _SECRET_A)
        assert routes[2].host == "g"

    def test_an_unreadable_database_is_none(self):
        with patch("cqc_lem.utilities.db.get_all_users_proxy_config", return_value=None):
            assert pf.load_routes_from_db() is None


class TestForwarding:
    async def test_connect_injects_the_users_credentials_and_tunnels(self):
        up = await (_Upstream()).start()
        fwd = await _forwarder({1: pf.parse_upstream(up.url("alice", _SECRET_A))})
        try:
            data = await _exchange(fwd.port_for(1), _CONNECT, then=b"hello")
            assert data.startswith(b"HTTP/1.1 200")
            assert data.endswith(b"echo:hello")
            head = up.heads[0]
            assert head.startswith("CONNECT www.linkedin.com:443 HTTP/1.1\r\n")
            assert f"Proxy-Authorization: {_basic('alice', _SECRET_A)}" in head
            assert fwd.stats["tunnels_ok"] == 1
        finally:
            await fwd.close()
            await up.stop()

    async def test_a_client_supplied_credential_is_replaced_not_forwarded(self):
        up = await (_Upstream()).start()
        fwd = await _forwarder({1: pf.parse_upstream(up.url("alice", _SECRET_A))})
        try:
            req = (b"CONNECT www.linkedin.com:443 HTTP/1.1\r\nHost: x\r\n"
                   b"Proxy-Authorization: Basic Zm9vOmJhcg==\r\n\r\n")
            await _exchange(fwd.port_for(1), req)
            assert "Zm9vOmJhcg==" not in up.heads[0]
            assert up.heads[0].count("Proxy-Authorization") == 1
        finally:
            await fwd.close()
            await up.stop()

    async def test_each_port_only_reaches_its_own_users_proxy(self):
        up_a = await (_Upstream()).start()
        up_b = await (_Upstream()).start()
        fwd = await _forwarder({1: pf.parse_upstream(up_a.url("alice", _SECRET_A)),
                                2: pf.parse_upstream(up_b.url("bob", _SECRET_B))})
        try:
            await _exchange(fwd.port_for(1), _CONNECT)
            await _exchange(fwd.port_for(1), _CONNECT)
            await _exchange(fwd.port_for(2), _CONNECT)
            assert len(up_a.heads) == 2 and len(up_b.heads) == 1
            assert all(_basic("alice", _SECRET_A) in h for h in up_a.heads)
            assert all(_SECRET_B not in h and _basic("bob", _SECRET_B) not in h for h in up_a.heads)
            assert _basic("bob", _SECRET_B) in up_b.heads[0]
        finally:
            await fwd.close()
            await up_a.stop()
            await up_b.stop()

    async def test_plain_http_is_forwarded_with_auth_and_connection_close(self):
        up = await (_Upstream(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")).start()
        fwd = await _forwarder({1: pf.parse_upstream(up.url("alice", _SECRET_A))})
        try:
            req = (b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n"
                   b"Proxy-Connection: keep-alive\r\nAccept: */*\r\n\r\n")
            data = await _exchange(fwd.port_for(1), req)
            assert data.startswith(b"HTTP/1.1 200 OK")
            head = up.heads[0]
            assert head.startswith("GET http://example.com/ HTTP/1.1\r\n")
            assert "Accept: */*" in head and "Connection: close" in head
            assert "Proxy-Connection" not in head
            assert f"Proxy-Authorization: {_basic('alice', _SECRET_A)}" in head
        finally:
            await fwd.close()
            await up.stop()

    async def test_an_authless_upstream_gets_no_auth_header(self):
        up = await (_Upstream()).start()
        fwd = await _forwarder({1: pf.parse_upstream(up.url())})
        try:
            await _exchange(fwd.port_for(1), _CONNECT)
            assert "Proxy-Authorization" not in up.heads[0]
        finally:
            await fwd.close()
            await up.stop()


class TestFailsClosed:
    async def test_a_port_with_no_route_answers_502(self):
        fwd = await _forwarder({})
        try:
            # Route removed between refreshes: the listener may still exist, the route does not.
            fwd._routes = {}
            server = await asyncio.start_server(
                lambda r, w: fwd._handle(1, r, w), "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            data = await _exchange(port, _CONNECT)
            server.close()
            assert data.startswith(b"HTTP/1.1 502")
            assert fwd.stats["no_route"] == 1
        finally:
            await fwd.close()

    async def test_an_unreachable_upstream_is_502_and_nothing_connects_direct(self):
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()
        fwd = await _forwarder({1: pf.parse_upstream(f"http://alice:{_SECRET_A}@127.0.0.1:{dead_port}")})
        real_open = asyncio.open_connection
        targets = []

        async def spy(host, port, **kw):
            targets.append((host, port))
            return await real_open(host, port, **kw)

        try:
            with patch.object(pf.asyncio, "open_connection", side_effect=spy):
                data = await _exchange(fwd.port_for(1), _CONNECT)
            assert data.startswith(b"HTTP/1.1 502")
            # The patch is module-global, so the test's own client connect is recorded too. What
            # matters: the dead upstream was tried, and nothing went to the destination directly.
            assert ("127.0.0.1", dead_port) in targets
            assert all(host == "127.0.0.1" for host, _ in targets)
            assert fwd.stats["upstream_unreachable"] == 1
        finally:
            await fwd.close()

    async def test_a_hanging_upstream_times_out_as_504(self):
        async def hang(reader, writer):
            await asyncio.sleep(10)

        server = await asyncio.start_server(hang, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        fwd = await _forwarder({1: pf.parse_upstream(f"http://127.0.0.1:{port}")})
        fwd._head_timeout = 0.3
        try:
            data = await _exchange(fwd.port_for(1), _CONNECT)
            assert data.startswith(b"HTTP/1.1 504")
        finally:
            await fwd.close()
            server.close()

    async def test_an_upstream_407_is_relayed_without_proxy_authenticate(self):
        up = await (_Upstream(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                              b"Proxy-Authenticate: Basic realm=\"x\"\r\n\r\n")).start()
        fwd = await _forwarder({1: pf.parse_upstream(up.url("alice", _SECRET_A))})
        try:
            data = await _exchange(fwd.port_for(1), _CONNECT)
            assert data.startswith(b"HTTP/1.1 407")
            assert b"Proxy-Authenticate" not in data
            assert fwd.stats["upstream_auth_failed"] == 1
        finally:
            await fwd.close()
            await up.stop()

    async def test_any_other_refusal_is_relayed(self):
        up = await (_Upstream(b"HTTP/1.1 403 Forbidden\r\n\r\n")).start()
        fwd = await _forwarder({1: pf.parse_upstream(up.url())})
        try:
            data = await _exchange(fwd.port_for(1), _CONNECT)
            assert data.startswith(b"HTTP/1.1 403")
            assert fwd.stats["upstream_refused"] == 1
        finally:
            await fwd.close()
            await up.stop()

    async def test_an_upstream_that_speaks_garbage_is_502(self):
        up = await (_Upstream(b"nonsense\r\n\r\n")).start()
        fwd = await _forwarder({1: pf.parse_upstream(up.url())})
        try:
            data = await _exchange(fwd.port_for(1), _CONNECT)
            assert data.startswith(b"HTTP/1.1 502")
        finally:
            await fwd.close()
            await up.stop()

    async def test_the_health_port_refuses_to_proxy(self):
        fwd = await _forwarder({})
        try:
            data = await _exchange(fwd._base, _CONNECT)
            assert data.startswith(b"HTTP/1.1 502")
        finally:
            await fwd.close()

    async def test_a_database_outage_keeps_the_last_routes(self):
        up = await (_Upstream()).start()
        state = {"routes": {1: pf.parse_upstream(up.url("alice", _SECRET_A))}}
        fwd = await _forwarder(lambda: state["routes"])
        try:
            state["routes"] = None
            with patch.object(pf, "log_warning") as warned:
                await fwd.refresh()
                await fwd.refresh()
            warned.assert_called_once()  # once per outage, not once per refresh
            assert fwd.listening_user_ids == [1]
            data = await _exchange(fwd.port_for(1), _CONNECT)
            assert data.startswith(b"HTTP/1.1 200")
        finally:
            await fwd.close()
            await up.stop()

    async def test_a_loader_that_raises_is_an_outage_not_a_crash(self):
        def boom():
            raise RuntimeError("db")

        fwd = pf.ProxyForwarder(boom, bind_host="127.0.0.1", base=_free_base())
        await fwd.refresh()
        assert fwd.listening_user_ids == [] and fwd.health()["source_failing"] is True

    @pytest.mark.parametrize("request_bytes", [
        b"CONNECT evil.example:443\r\nInjected: x HTTP/1.1\r\n\r\n",
        b"CONNECT bad target:443 HTTP/1.1\r\n\r\n",
        b"CONNECT no-port HTTP/1.1\r\n\r\n",
        b"GET /local HTTP/1.1\r\nHost: x\r\n\r\n",
        b"GARBAGE\r\n\r\n",
        b"CONNECT a:1 HTTP/2.0\r\n\r\n",
        b"GET http://example.com/ HTTP/1.1\r\nX-A: 1\nProxy-Authorization: Basic eA==\r\n\r\n",
        b"GET http://example.com/\x00 HTTP/1.1\r\nHost: x\r\n\r\n",
    ])
    async def test_malformed_requests_are_400_and_never_reach_upstream(self, request_bytes):
        up = await (_Upstream()).start()
        fwd = await _forwarder({1: pf.parse_upstream(up.url("alice", _SECRET_A))})
        try:
            data = await _exchange(fwd.port_for(1), request_bytes)
            assert data.startswith(b"HTTP/1.1 400")
            assert up.heads == []
        finally:
            await fwd.close()
            await up.stop()


class TestListeners:
    async def test_a_removed_user_loses_their_listener(self):
        up = await (_Upstream()).start()
        state = {"routes": {1: pf.parse_upstream(up.url()), 2: pf.parse_upstream(up.url())}}
        fwd = await _forwarder(lambda: state["routes"])
        try:
            assert fwd.listening_user_ids == [1, 2]
            state["routes"] = {1: state["routes"][1]}
            await fwd.refresh()
            assert fwd.listening_user_ids == [1]
        finally:
            await fwd.close()
            await up.stop()

    async def test_a_changed_proxy_takes_effect_on_the_next_refresh(self):
        up_old = await (_Upstream()).start()
        up_new = await (_Upstream()).start()
        state = {"routes": {1: pf.parse_upstream(up_old.url("alice", _SECRET_A))}}
        fwd = await _forwarder(lambda: state["routes"])
        try:
            state["routes"] = {1: pf.parse_upstream(up_new.url("alice", _SECRET_A))}
            await fwd.refresh()
            await _exchange(fwd.port_for(1), _CONNECT)
            assert up_old.heads == [] and len(up_new.heads) == 1
        finally:
            await fwd.close()
            await up_old.stop()
            await up_new.stop()

    async def test_users_over_the_cap_get_no_listener(self):
        up = await (_Upstream()).start()
        routes = {uid: pf.parse_upstream(up.url()) for uid in (1, 2, 3)}
        fwd = await _forwarder(routes, max_listeners=2)
        try:
            assert fwd.listening_user_ids == [1, 2]
        finally:
            await fwd.close()
            await up.stop()

    async def test_serve_forever_refreshes_until_cancelled(self):
        calls = []
        fwd = pf.ProxyForwarder(lambda: calls.append(1) or {}, bind_host="127.0.0.1",
                                base=_free_base())
        task = asyncio.ensure_future(fwd.serve_forever(refresh_seconds=0))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls  # refreshed at least once


class TestNoCredentialInLogsOrHealth:
    async def test_no_log_line_or_health_field_carries_a_credential(self):
        ok = await (_Upstream()).start()
        refused = await (_Upstream(b"HTTP/1.1 407 Nope\r\n\r\n")).start()
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()
        routes = {1: pf.parse_upstream(ok.url("alice", _SECRET_A)),
                  2: pf.parse_upstream(refused.url("bob", _SECRET_B)),
                  3: pf.parse_upstream(f"http://carol:{_SECRET_A}@127.0.0.1:{dead_port}")}
        lines = []

        def capture(*args, **kwargs):
            lines.append(repr(args) + repr(kwargs))

        with patch.object(pf, "log_debug", side_effect=capture), \
                patch.object(pf, "log_info", side_effect=capture), \
                patch.object(pf, "log_warning", side_effect=capture):
            fwd = await _forwarder(routes)
            try:
                for uid in (1, 2, 3):
                    await _exchange(fwd.port_for(uid), _CONNECT)
                body = await _exchange(fwd._base, b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
            finally:
                await fwd.close()
                await ok.stop()
                await refused.stop()
        assert lines, "expected at least the debug line for the refused tunnel"
        for text in lines + [body.decode("latin-1"), json.dumps(fwd.health())]:
            for secret in (_SECRET_A, _SECRET_B, "alice", "bob", "carol",
                           _basic("alice", _SECRET_A), _basic("bob", _SECRET_B)):
                assert secret not in text


class TestHealth:
    async def test_health_answers_200_with_counts_after_the_first_refresh(self):
        fwd = await _forwarder({})
        try:
            data = await _exchange(fwd._base, b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
            assert data.startswith(b"HTTP/1.1 200")
            assert await asyncio.to_thread(pf.check, "127.0.0.1", fwd._base, 2) == 0
        finally:
            await fwd.close()

    async def test_health_is_503_before_routes_load(self):
        fwd = pf.ProxyForwarder(lambda: {}, bind_host="127.0.0.1", base=_free_base())
        await fwd.start_health()
        try:
            assert await asyncio.to_thread(pf.check, "127.0.0.1", fwd._base, 2) == 1
            assert fwd.health()["status"] == "starting"
        finally:
            await fwd.close()

    def test_check_with_nothing_listening_is_1(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        assert pf.check("127.0.0.1", port, 1) == 1

    def test_main_check_flag_runs_the_check(self):
        with patch.object(pf, "check", return_value=0) as checked:
            assert pf.main(["--check"]) == 0
        checked.assert_called_once()

    def test_main_runs_the_forwarder_until_interrupted(self):
        def stop(coro):
            coro.close()
            raise KeyboardInterrupt

        with patch.object(pf.asyncio, "run", side_effect=stop):
            assert pf.main([]) == 0
