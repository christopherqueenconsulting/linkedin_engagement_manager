"""`McpGate`, phase P1: every check answers JSON, in order, and nothing gets past auth yet.

Plan §3.3 steps 1-6 (`docs/agent-mcp-plan.md`). No `mcp`-scoped session can exist in P1, so any
bearer that survives the HTTP checks is refused with 401 — the endpoint is mounted but inert.
"""

import asyncio
import contextlib
import logging
import socket
import time
from typing import Any, Iterator
from unittest.mock import patch

import pytest
import redis

from cqc_lem.api.mcp import gate as gate_mod, limits
from cqc_lem.api.mcp.gate import McpGate
from cqc_lem.api.mcp.lifespan import MAX_REQUEST_BODY_BYTES, mcp_state

from .conftest import FakeRedis

pytestmark = pytest.mark.unit

# Taken at import, before the unit lane's autouse guard patches `from_url` to raise, so the one
# test that needs a real client on a local socket can put it back for its own duration.
_REAL_FROM_URL = redis.Redis.__dict__["from_url"]

_JSON = {"Content-Type": "application/json"}
_BODY = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
# Distinctive enough that finding it in any log record can only mean it leaked, and deliberately
# LOW-entropy: a random-looking literal here reads as a committed secret to the GitGuardian scan.
_TOKEN = "lem_mcp_" + "canary" * 4


def _post(client: Any, path: str = "/api/mcp", **kwargs: Any) -> Any:
    headers = {**_JSON, **kwargs.pop("headers", {})}
    return client.post(path, json=kwargs.pop("json", _BODY), headers=headers,
                       follow_redirects=False, **kwargs)


def _server_side(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    """Drop the TEST CLIENT's own request log, which prints the URL it was asked to send.

    That record is the caller's side. Prod uvicorn runs `--no-access-log`, and nginx's line for a query-string
    `/api/mcp` is switched off (`compose/prod/nginx/default.conf.tmpl`).
    """
    kept = [r for r in records if not r.name.startswith("httpx")]
    assert kept, "no server-side record was captured, so the leak check would prove nothing"
    return kept


def _assert_refusal(resp: Any, status: int, reason: str) -> None:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["status_code"] == status
    assert body["reason"] == reason


class TestUnauthenticatedIs401Json:
    def test_no_credential_is_401_json_never_a_redirect(self, api_client: Any,
                                                         fake_redis: FakeRedis) -> None:
        resp = _post(api_client)
        _assert_refusal(resp, 401, "missing_token")
        assert resp.headers["www-authenticate"] == "Bearer"
        assert fake_redis.keys_read == ["mcp:kill"]

    @pytest.mark.parametrize("token", [_TOKEN, "session-token-abc", "x" * 64])
    def test_every_bearer_is_401_in_p1(self, api_client: Any, fake_redis: FakeRedis,
                                       token: str) -> None:
        """Agent, observer, operator, full or junk: no token can be an mcp session yet."""
        resp = _post(api_client, headers={"Authorization": f"Bearer {token}"})
        _assert_refusal(resp, 401, "invalid_token")
        assert resp.headers["www-authenticate"] == 'Bearer error="invalid_token"'
        assert "resource_metadata" not in resp.headers["www-authenticate"]
        assert "Agents" in resp.json()["detail"]
        assert token not in resp.text

    def test_a_non_bearer_scheme_counts_as_no_token(self, api_client: Any,
                                                    fake_redis: FakeRedis) -> None:
        resp = _post(api_client, headers={"Authorization": "Basic dXNlcjpwYXNz"})
        _assert_refusal(resp, 401, "missing_token")

    def test_a_browser_cookie_alone_is_cookie_only(self, api_client: Any,
                                                   fake_redis: FakeRedis) -> None:
        resp = _post(api_client, headers={"Cookie": "other=1; lem_session=abc"})
        _assert_refusal(resp, 401, "cookie_only")

    def test_a_cookie_is_ignored_when_a_bearer_is_present(self, api_client: Any,
                                                          fake_redis: FakeRedis) -> None:
        resp = _post(api_client, headers={"Cookie": "lem_session=abc",
                                          "Authorization": f"Bearer {_TOKEN}"})
        _assert_refusal(resp, 401, "invalid_token")

    def test_a_similarly_named_cookie_is_not_a_session(self, api_client: Any,
                                                       fake_redis: FakeRedis) -> None:
        resp = _post(api_client, headers={"Cookie": "not_lem_session=abc"})
        _assert_refusal(resp, 401, "missing_token")

    def test_the_response_is_no_store(self, api_client: Any, fake_redis: FakeRedis) -> None:
        assert "no-store" in _post(api_client).headers["cache-control"]


class TestHttpChecksAnswerBeforeAuth:
    @pytest.mark.parametrize("method", ["GET", "DELETE", "PUT", "PATCH", "OPTIONS"])
    def test_any_method_but_post_is_405_json(self, api_client: Any, fake_redis: FakeRedis,
                                             method: str) -> None:
        resp = api_client.request(method, "/api/mcp",
                                  headers={"Authorization": f"Bearer {_TOKEN}"})
        _assert_refusal(resp, 405, "method_not_allowed")
        assert resp.headers["allow"] == "POST"

    def test_a_foreign_host_is_421(self, api_client: Any, fake_redis: FakeRedis) -> None:
        _assert_refusal(_post(api_client, headers={"Host": "evil.example"}), 421,
                        "host_not_allowed")

    def test_loopback_is_not_allowed_on_a_public_base_url(self, api_client: Any,
                                                          fake_redis: FakeRedis) -> None:
        _assert_refusal(_post(api_client, headers={"Host": "localhost:8000"}), 421,
                        "host_not_allowed")

    def test_the_configured_host_with_its_default_port_passes(
            self, api_client: Any, fake_redis: FakeRedis) -> None:
        _assert_refusal(_post(api_client, headers={"Host": "TESTSERVER:80"}), 401,
                        "missing_token")

    def test_a_foreign_origin_is_403(self, api_client: Any, fake_redis: FakeRedis) -> None:
        _assert_refusal(_post(api_client, headers={"Origin": "https://evil.example"}), 403,
                        "origin_not_allowed")

    def test_the_own_origin_passes(self, api_client: Any, fake_redis: FakeRedis) -> None:
        _assert_refusal(_post(api_client, headers={"Origin": "http://testserver"}), 401,
                        "missing_token")

    @pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded",
                                              "application/jsonp"])
    def test_a_non_json_content_type_is_415(self, api_client: Any, fake_redis: FakeRedis,
                                            content_type: str) -> None:
        resp = api_client.post("/api/mcp", content=b"{}",
                               headers={"Content-Type": content_type})
        _assert_refusal(resp, 415, "unsupported_media_type")

    def test_a_missing_content_type_is_415(self, api_client: Any, fake_redis: FakeRedis) -> None:
        _assert_refusal(api_client.post("/api/mcp", content=b"{}"), 415,
                        "unsupported_media_type")

    def test_json_with_a_charset_passes(self, api_client: Any, fake_redis: FakeRedis) -> None:
        resp = api_client.post("/api/mcp", content=b"{}",
                               headers={"Content-Type": "application/json; charset=utf-8"})
        _assert_refusal(resp, 401, "missing_token")

    def test_a_declared_body_over_256kb_is_413(self, api_client: Any,
                                               fake_redis: FakeRedis) -> None:
        resp = api_client.post("/api/mcp", content=b"x" * (MAX_REQUEST_BODY_BYTES + 1),
                               headers=_JSON)
        _assert_refusal(resp, 413, "request_too_large")

    def test_a_streamed_body_over_256kb_is_413(self, api_client: Any,
                                               fake_redis: FakeRedis) -> None:
        def _chunks() -> Iterator[bytes]:
            for _ in range(5):
                yield b"x" * (64 * 1024)

        resp = api_client.post("/api/mcp", content=_chunks(), headers=_JSON)
        assert "content-length" not in {k.lower() for k in resp.request.headers}
        _assert_refusal(resp, 413, "request_too_large")

    def test_a_body_exactly_at_the_cap_is_read_and_reaches_auth(
            self, api_client: Any, fake_redis: FakeRedis) -> None:
        resp = api_client.post("/api/mcp", content=b"x" * MAX_REQUEST_BODY_BYTES, headers=_JSON)
        _assert_refusal(resp, 401, "missing_token")

    def test_the_checks_run_before_auth(self, api_client: Any, fake_redis: FakeRedis) -> None:
        """A wrong Host with a bearer is still 421: the gate never gets as far as the token."""
        resp = _post(api_client, headers={"Host": "evil.example",
                                          "Authorization": f"Bearer {_TOKEN}"})
        _assert_refusal(resp, 421, "host_not_allowed")


class TestQueryStringToken:
    @pytest.mark.parametrize("param", ["token", "session_token", "access_token"])
    def test_a_token_in_the_url_is_400_and_never_logged(
            self, api_client: Any, fake_redis: FakeRedis, param: str,
            caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG, logger="cqc-lem"):
            resp = _post(api_client, f"/api/mcp?{param}={_TOKEN}",
                         headers={"Authorization": f"Bearer {_TOKEN}"})
        _assert_refusal(resp, 400, "token_in_url_refused")
        assert _TOKEN not in resp.text
        refused = [r for r in caplog.records if "token in query string" in r.getMessage()]
        assert [r.levelno for r in refused] == [logging.INFO]
        assert all(_TOKEN not in r.getMessage() and _TOKEN not in str(r.__dict__)
                   for r in _server_side(caplog.records))

    def test_an_unrelated_query_parameter_is_not_a_token(self, api_client: Any,
                                                         fake_redis: FakeRedis) -> None:
        _assert_refusal(_post(api_client, "/api/mcp?client=claude"), 401, "missing_token")


class TestKillSwitch:
    def test_the_env_switch_is_503_and_reads_no_redis(
            self, api_client: Any, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SERVER_KILL", "1")
        _assert_refusal(_post(api_client), 503, "mcp_disabled_by_operator")
        assert fake_redis.keys_read == []

    @pytest.mark.parametrize("value", ["0", "false", "off", ""])
    def test_an_off_env_value_does_not_kill(self, api_client: Any, fake_redis: FakeRedis,
                                            monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("MCP_SERVER_KILL", value)
        _assert_refusal(_post(api_client), 401, "missing_token")

    @pytest.mark.parametrize("stored", [b"1", b"0", b""])
    def test_any_stored_kill_key_is_503(self, api_client: Any, fake_redis: FakeRedis,
                                        stored: bytes) -> None:
        fake_redis.kill_value = stored
        _assert_refusal(_post(api_client), 503, "mcp_disabled_by_operator")

    def test_a_raising_redis_fails_closed(self, api_client: Any, fake_redis: FakeRedis) -> None:
        fake_redis.raises = True
        _assert_refusal(_post(api_client), 503, "mcp_unavailable")

    def test_no_redis_handle_fails_closed(self, api_client: Any) -> None:
        with patch("cqc_lem.api.mcp.limits._redis", return_value=None):
            _assert_refusal(_post(api_client), 503, "mcp_unavailable")

    def test_the_env_switch_wins_over_every_check(
            self, api_client: Any, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SERVER_KILL", "1")
        resp = api_client.get("/api/mcp", headers={"Host": "evil.example"})
        _assert_refusal(resp, 503, "mcp_disabled_by_operator")

    def test_the_redis_key_refuses_everything_past_the_header_checks(
            self, api_client: Any, fake_redis: FakeRedis) -> None:
        """Before the body, the query-token check and auth: nothing it refuses gets further."""
        fake_redis.kill_value = b"1"

        def _chunks() -> Iterator[bytes]:
            for _ in range(5):
                yield b"x" * (64 * 1024)

        resp = api_client.post(f"/api/mcp?token={_TOKEN}", content=_chunks(),
                               headers={**_JSON, "Authorization": f"Bearer {_TOKEN}"})
        _assert_refusal(resp, 503, "mcp_disabled_by_operator")

    @pytest.mark.parametrize("method,headers,content,status", [
        ("GET", {}, None, 405),
        ("POST", {**_JSON, "Host": "evil.example"}, b"{}", 421),
        ("POST", {**_JSON, "Origin": "https://evil.example"}, b"{}", 403),
        ("POST", {"Content-Type": "text/plain"}, b"{}", 415),
        ("POST", _JSON, b"x" * (MAX_REQUEST_BODY_BYTES + 1), 413),
    ])
    def test_junk_traffic_is_refused_without_reading_redis(
            self, api_client: Any, fake_redis: FakeRedis, method: str, headers: dict,
            content: Any, status: int) -> None:
        """The header-only checks run first, so junk never holds a threadpool thread on Redis."""
        resp = api_client.request(method, "/api/mcp", headers=headers, content=content)
        assert resp.status_code == status
        assert fake_redis.keys_read == []



class TestKillClient:
    """The kill read's own Redis client: short timeouts, no retry, cached per (pid, url)."""

    @pytest.fixture(autouse=True)
    def _fresh_client(self) -> Iterator[None]:
        limits.reset_kill_client()
        yield
        limits.reset_kill_client()

    def test_it_is_built_short_and_never_retries(self) -> None:
        with patch("redis.Redis.from_url", return_value=FakeRedis(kill_value=b"1")) as build:
            assert limits.read_kill_key() is limits.KillState.ON
            assert limits.read_kill_key() is limits.KillState.ON
        assert build.call_count == 1, "the client is cached per (pid, url)"
        kwargs = build.call_args.kwargs
        assert kwargs["socket_timeout"] <= 1 and kwargs["socket_connect_timeout"] <= 1
        assert kwargs["retry"]._retries == 0

    def test_it_takes_the_url_from_the_shared_resolution_rule(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CELERY_BROKER_URL", "redis://kill-host:6390/3")
        with patch("redis.Redis.from_url", return_value=FakeRedis()) as build:
            assert limits.read_kill_key() is limits.KillState.OFF
        assert build.call_args.args[0] == "redis://kill-host:6390/3"

    def test_a_client_that_cannot_be_built_is_unreadable_and_not_cached(self) -> None:
        # The unit lane's hermetic guard makes `from_url` raise, which is exactly this case.
        assert limits.read_kill_key() is limits.KillState.UNREADABLE
        with patch("redis.Redis.from_url", return_value=FakeRedis()):
            assert limits.read_kill_key() is limits.KillState.OFF

    def test_no_redis_package_is_unreadable(self) -> None:
        with patch.dict("sys.modules", {"redis": None}):
            assert limits._redis() is None


@contextlib.contextmanager
def _silent_redis() -> Iterator[str]:
    """A local TCP listener that accepts and never answers: Redis hung, not refused."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    try:
        yield f"redis://127.0.0.1:{server.getsockname()[1]}/0"
    finally:
        server.close()


class TestUnreachableRedisIsAFast503:
    """With Redis hung or gone, the gate still answers inside about a second, failing closed.

    A REAL redis-py client on a real socket: the unit lane's hermetic guard is lifted for this test
    only, and the socket is local, so nothing leaves the machine.
    """

    @pytest.fixture(autouse=True)
    def _real_redis_client(self) -> Iterator[None]:
        limits.reset_kill_client()
        with patch.object(redis.Redis, "from_url", _REAL_FROM_URL):
            yield
        limits.reset_kill_client()

    def test_a_hung_redis_is_503_within_a_second(self, api_client: Any,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
        with _silent_redis() as url:
            monkeypatch.setenv("CELERY_BROKER_URL", url)
            started = time.monotonic()
            resp = _post(api_client)
            elapsed = time.monotonic() - started
        _assert_refusal(resp, 503, "mcp_unavailable")
        assert elapsed < 1.5, f"kill read took {elapsed:.2f}s against a hung Redis"

    def test_a_refused_redis_is_503_within_a_second(self, api_client: Any,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
        with _silent_redis() as url:
            pass  # closed again: nothing listens on that port now
        monkeypatch.setenv("CELERY_BROKER_URL", url)
        started = time.monotonic()
        resp = _post(api_client)
        elapsed = time.monotonic() - started
        _assert_refusal(resp, 503, "mcp_unavailable")
        assert elapsed < 1.5, f"kill read took {elapsed:.2f}s against a refused port"


class TestPathMatching:
    def test_the_route_hands_every_method_to_the_gate_class(self) -> None:
        """The route holds the gate CLASS and passes it every method.

        The class, so route-table guards can `inspect.getsource` it; every method, so the 405 is
        the gate's JSON one.
        """
        import inspect

        import cqc_lem.api.main as main_mod

        routes = [r for r in main_mod._walk_routes(main_mod.app.routes)
                  if getattr(r, "path", None) == "/api/mcp"]
        assert len(routes) == 1
        assert routes[0].endpoint is McpGate and routes[0].methods is None
        assert "class McpGate" in inspect.getsource(routes[0].endpoint)

    def test_only_the_exact_path_is_public(self) -> None:
        import cqc_lem.api.main as main_mod

        assert main_mod._is_public_api_path("/api/mcp") is True
        for path in ("/api/mcp/", "/api/mcp/x", "/api/mcp-x", "/api/mcpadmin"):
            assert main_mod._is_public_api_path(path) is False, path
        assert "/api/mcp" not in main_mod._PUBLIC_API_PREFIXES

    @pytest.mark.parametrize("path", ["/api/mcp/x", "/api/mcp-x", "/api/mcpadmin", "/api/mcp/"])
    def test_neighbouring_paths_stay_behind_the_edge_gate(
            self, api_client: Any, fake_redis: FakeRedis, path: str) -> None:
        import cqc_lem.api.main as main_mod

        with patch.object(main_mod, "_API_ACCESS_TOKEN_SET", {"edge-token"}):
            neighbour = _post(api_client, path)
            exact = _post(api_client)
        assert neighbour.status_code == 401
        assert neighbour.json() == {"status_code": 401, "detail": "Unauthorized"}
        _assert_refusal(exact, 401, "missing_token")

    @pytest.mark.parametrize("ui_built,expected", [(False, 307), (True, 405)])
    def test_trailing_slash_never_reaches_the_gate(self, api_client: Any, fake_redis: FakeRedis,
                                                   ui_built: bool, expected: int) -> None:
        """`POST /api/mcp/` serves nothing in either deploy shape.

        CI (no UI build): `redirect_slashes` answers 307 to the gate, serving nothing itself.
        Prod (UI build): the SPA catch-all claims the path for GET only, so a POST is a 405.
        The catch-all is set up here either way, so a local `ui/dist` cannot flip the answer.
        """
        from cqc_lem.api.main import app

        before = list(app.router.routes)

        async def _spa(full_path: str) -> dict[str, str]:
            return {"spa": full_path}

        app.router.routes[:] = [r for r in before
                                if getattr(r, "path", None) != "/{full_path:path}"]
        if ui_built:
            app.add_api_route("/{full_path:path}", _spa, methods=["GET"], include_in_schema=False)
        try:
            resp = _post(api_client, "/api/mcp/")
            exact = _post(api_client)
        finally:
            app.router.routes[:] = before
        assert resp.status_code == expected
        if expected == 307:
            assert resp.headers["location"].endswith("/api/mcp")
        _assert_refusal(exact, 401, "missing_token")


class TestWellKnown:
    @pytest.mark.parametrize("path", [
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/api/mcp",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-authorization-server/api/mcp",
    ])
    def test_oauth_discovery_is_a_json_404_with_no_store(self, api_client: Any,
                                                         path: str) -> None:
        resp = api_client.get(path)
        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith("application/json")
        assert "no-store" in resp.headers["cache-control"]


class TestDisconnectThroughTheFullStack:
    """A client that hangs up mid-body must not become an `$exception` (review of #2695).

    `TestClient` cannot disconnect mid-body (its `receive` waits for the response), so this drives
    the REAL `cqc_lem.api.main.app` — every middleware included — as ASGI, while `api_client` holds
    the lifespan up. Before the fix the gate returned without a response, `BaseHTTPMiddleware`
    raised "No response returned." and `observability_middleware` filed it.
    """

    def test_a_mid_body_disconnect_files_no_exception(self, api_client: Any,
                                                      fake_redis: FakeRedis) -> None:
        from cqc_lem.api.main import app

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": "/api/mcp", "raw_path": b"/api/mcp",
            "query_string": b"", "root_path": "", "client": ("203.0.113.9", 5555),
            "server": ("testserver", 80),
            "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
        }
        inbox = [{"type": "http.request", "body": b"{", "more_body": True},
                 {"type": "http.disconnect"}]
        sent: list[dict] = []

        async def _receive() -> dict[str, Any]:
            return inbox.pop(0) if inbox else {"type": "http.disconnect"}

        async def _send(message: dict[str, Any]) -> None:
            sent.append(message)

        async def _drive() -> None:
            await app(scope, _receive, _send)

        with patch("cqc_lem.api.main.capture_exception") as captured:
            asyncio.run(_drive())
        captured.assert_not_called()
        starts = [m for m in sent if m["type"] == "http.response.start"]
        assert [m["status"] for m in starts] == [400]


class TestLogging:
    def test_refusals_log_at_debug_and_never_carry_the_token(
            self, api_client: Any, fake_redis: FakeRedis,
            caplog: pytest.LogCaptureFixture) -> None:
        auth = {"Authorization": f"Bearer {_TOKEN}"}
        with caplog.at_level(logging.DEBUG, logger="cqc-lem"):
            _post(api_client, headers=auth)
            _post(api_client, headers={**auth, "Host": "evil.example"})
            api_client.get("/api/mcp", headers=auth)
            api_client.post("/api/mcp", content=b"{}", headers=auth)
        gate_records = [r for r in caplog.records if r.getMessage().startswith("MCP request")]
        assert len(gate_records) == 4
        assert {r.levelno for r in gate_records} == {logging.DEBUG}
        for record in _server_side(caplog.records):
            assert _TOKEN not in record.getMessage()
            assert _TOKEN not in str(record.__dict__)


def _run_gate(scope_overrides: dict[str, Any], messages: list[dict[str, Any]]) -> list[dict]:
    """Drive `McpGate` directly with a hand-built scope: for inputs no HTTP client will send."""
    scope: dict[str, Any] = {
        "type": "http", "method": "POST", "path": "/api/mcp", "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
    }
    scope.update(scope_overrides)
    inbox = list(messages)
    sent: list[dict] = []

    async def _receive() -> dict[str, Any]:
        return inbox.pop(0)

    async def _send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def _drive() -> None:
        await McpGate(scope, _receive, _send)

    asyncio.run(_drive())
    return sent


class TestGateDirect:
    @pytest.fixture(autouse=True)
    def _up(self, fake_redis: FakeRedis) -> Iterator[None]:
        saved = (mcp_state.unavailable, mcp_state.host, mcp_state.origin, mcp_state.dev_loopback)
        mcp_state.unavailable, mcp_state.host, mcp_state.origin, mcp_state.dev_loopback = (
            False, "testserver", "http://testserver", False)
        yield
        (mcp_state.unavailable, mcp_state.host, mcp_state.origin,
         mcp_state.dev_loopback) = saved

    def test_a_non_numeric_content_length_is_400(self) -> None:
        sent = _run_gate({"headers": [(b"host", b"testserver"),
                                      (b"content-type", b"application/json"),
                                      (b"content-length", b"lots")]}, [])
        assert sent[0]["status"] == 400

    def test_a_client_that_disconnects_mid_body_still_gets_a_response(self) -> None:
        sent = _run_gate({}, [{"type": "http.request", "body": b"{", "more_body": True},
                              {"type": "http.disconnect"}])
        assert sent[0]["status"] == 400
        assert b"client_disconnected" in sent[1]["body"]

    def test_no_lifespan_means_503(self) -> None:
        mcp_state.unavailable = True
        sent = _run_gate({}, [{"type": "http.request", "body": b"{}"}])
        assert sent[0]["status"] == 503

    def test_a_missing_host_header_is_421(self) -> None:
        sent = _run_gate({"headers": [(b"content-type", b"application/json")]},
                         [{"type": "http.request", "body": b"{}"}])
        assert sent[0]["status"] == 421

    @pytest.mark.parametrize("host,origin,expected", [
        ("localhost:5173", None, 401),
        ("127.0.0.1", None, 401),
        ("[::1]:8000", None, 401),
        ("localhost:8000", "http://localhost:5173", 401),
        ("localhost:8000", "http://127.0.0.1", 401),
        ("evil.example", None, 421),
        ("localhost:8000", "http://[::1]:5173", 401),
        ("localhost:8000", "https://evil.example", 403),
    ])
    def test_a_loopback_base_url_accepts_loopback(self, host: str, origin: Any,
                                                  expected: int) -> None:
        mcp_state.host, mcp_state.origin, mcp_state.dev_loopback = (
            "localhost:8000", "http://localhost:8000", True)
        headers = [(b"host", host.encode()), (b"content-type", b"application/json")]
        if origin:
            headers.append((b"origin", origin.encode()))
        sent = _run_gate({"headers": headers}, [{"type": "http.request", "body": b"{}"}])
        assert sent[0]["status"] == expected

    def test_the_gate_reads_main_bearer_parser(self) -> None:
        assert gate_mod._bearer("Bearer abc") == "abc"
        assert gate_mod._bearer("bearer   ") is None
        assert gate_mod._bearer(None) is None
