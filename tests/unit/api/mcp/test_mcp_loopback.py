"""The in-process loopback (`api/mcp/loopback.py`, plan §2, §3.4, §9 `test_mcp_loopback.py`)."""

import asyncio
import contextvars
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import httpx2
import pytest

from cqc_lem.api.mcp import calls, loopback as lb_mod
from cqc_lem.api.mcp.dispatch import DISPATCH_HEADER, dispatch_secret
from cqc_lem.api.mcp.gate import McpPrincipal
from cqc_lem.api.mcp.loopback import FAN_OUT_CONCURRENCY, Loopback, ToolFailure

from ._admitted import TOKEN, admitted, call, structured

pytestmark = pytest.mark.unit

_PRINCIPAL = McpPrincipal(user_id=77, session_id=901, scope="mcp", expires_at=None,
                          client_name="test")
_PROBE: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("lem_test_probe",
                                                                        default=None)


class _Recording(httpx2.AsyncBaseTransport):
    """A transport that records each request and answers a canned 200 (or `status`)."""

    def __init__(self, status: int = 200, body: Any = None, delay: float = 0.0) -> None:
        self.requests: list[httpx2.Request] = []
        self.status, self.body, self.delay = status, body, delay
        self.in_flight = self.peak = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        _PROBE.set("set-inside-the-inner-request")
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        return httpx2.Response(self.status, json={"status_code": self.status,
                                                  "detail": self.body})


def _loopback(transport: httpx2.AsyncBaseTransport) -> Loopback:
    lb = Loopback(principal=_PRINCIPAL, token=TOKEN, client_ip="203.0.113.9")
    lb._client = httpx2.AsyncClient(transport=transport, base_url="http://testserver",
                                    headers=lb._headers())
    return lb


@pytest.fixture
def no_budget_limit() -> Any:
    with patch.object(lb_mod, "consume_budgets", return_value=None) as spend:
        yield spend


class TestTheTransport:
    def test_it_is_an_in_process_asgi_transport_never_a_network_one(self) -> None:
        transport = lb_mod.build_transport()
        assert type(transport) is httpx2.ASGITransport
        from cqc_lem.api.main import app

        assert transport.app is app

    async def test_the_client_it_builds_uses_that_transport(self) -> None:
        lb = Loopback(principal=_PRINCIPAL, token=TOKEN)
        client = lb._http()
        try:
            assert type(client._transport) is httpx2.ASGITransport
        finally:
            await lb.aclose()

    def test_the_token_is_not_in_its_repr(self) -> None:
        assert TOKEN not in repr(Loopback(principal=_PRINCIPAL, token=TOKEN))


class TestTheInnerRequest:
    async def test_headers_and_the_cookie_sentinel(self, no_budget_limit: MagicMock) -> None:
        transport = _Recording(body={"ok": 1})
        lb = _loopback(transport)
        assert await lb.get("/user/settings", {"session_token": TOKEN, "x": None}) == {"ok": 1}
        request = transport.requests[0]
        assert request.headers["X-LEM-Client"] == "mcp"
        assert request.headers[DISPATCH_HEADER] == dispatch_secret()
        assert request.headers["Cookie"] == f"lem_session={TOKEN}"
        assert request.headers["CF-Connecting-IP"] == "203.0.113.9"
        assert request.url.params["session_token"] == "cookie", "a caller value is replaced"
        assert TOKEN not in str(request.url), "the token is in no URL"
        assert "x" not in request.url.params
        assert request.url.path == "/api/user/settings"
        await lb.aclose()

    async def test_a_trailing_slash_route_is_called_by_its_real_path(
            self, no_budget_limit: MagicMock) -> None:
        transport = _Recording(body=[])
        lb = _loopback(transport)
        await lb.get("/activity")
        assert transport.requests[0].url.path == "/api/activity/"
        await lb.aclose()

    async def test_no_contextvar_leaks_out_of_an_inner_call(self, no_budget_limit: MagicMock) -> None:
        lb = _loopback(_Recording(body={}))
        await lb.get("/user/settings")
        assert _PROBE.get() is None
        await lb.aclose()

    async def test_fan_out_runs_at_most_four_at_once(self, no_budget_limit: MagicMock) -> None:
        transport = _Recording(body={}, delay=0.02)
        lb = _loopback(transport)
        await asyncio.gather(*(lb.get("/user/settings") for _ in range(10)))
        assert FAN_OUT_CONCURRENCY == 4
        assert transport.peak == 4
        assert lb.inner_calls == 10
        await lb.aclose()

    @pytest.mark.parametrize("status, outcome, code", [
        (500, "upstream_error", "upstream_error"), (503, "upstream_error", "unavailable"),
        (404, "refused", "not_found"), (403, "refused", "forbidden"),
        (422, "invalid_args", "invalid_arguments"), (429, "rate_limited", "rate_limited"),
        (409, "refused", "http_409"),
    ])
    async def test_status_mapping(self, no_budget_limit: MagicMock, status: int, outcome: str,
                                  code: str) -> None:
        lb = _loopback(_Recording(status=status, body=[{"input": "echo"}]))
        with pytest.raises(ToolFailure) as raised:
            await lb.get("/user/settings")
        assert (raised.value.outcome, raised.value.code, raised.value.inner_status) == (
            outcome, code, status)
        assert "echo" not in raised.value.message
        await lb.aclose()

    async def test_a_route_code_is_kept(self, no_budget_limit: MagicMock) -> None:
        lb = _loopback(_Recording(status=403, body={"code": "session_scope_forbidden",
                                                    "message": "no"}))
        with pytest.raises(ToolFailure) as raised:
            await lb.get("/user/settings")
        assert raised.value.code == "session_scope_forbidden"
        await lb.aclose()


class TestTheReadBudget:
    async def test_each_inner_call_spends_the_read_budgets(self, no_budget_limit: MagicMock) -> None:
        lb = _loopback(_Recording(body={}))
        await lb.get("/user/settings")
        await lb.get("/user/settings")
        assert no_budget_limit.call_count == 2
        assert no_budget_limit.call_args.args == (77, lb_mod.READ_BUDGETS)
        await lb.aclose()

    async def test_a_spent_budget_refuses_before_the_request(self) -> None:
        transport = _Recording(body={})
        lb = _loopback(transport)
        with patch.object(lb_mod, "consume_budgets", return_value=30):
            with pytest.raises(ToolFailure) as raised:
                await lb.get("/user/settings")
        assert raised.value.outcome == "rate_limited"
        assert transport.requests == []
        await lb.aclose()

    async def test_unreadable_limits_fail_closed(self) -> None:
        transport = _Recording(body={})
        lb = _loopback(transport)
        with patch.object(lb_mod, "consume_budgets", side_effect=lb_mod.LimitsUnavailable()):
            with pytest.raises(ToolFailure) as raised:
                await lb.get("/user/settings")
        assert raised.value.code == "mcp_unavailable"
        assert transport.requests == []
        await lb.aclose()

    def test_a_zero_read_budget_refuses_every_tool_call(
            self, api_client: Any, fake_redis: Any, resolver: MagicMock,
            monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SERVER_ENABLED", "true")
        monkeypatch.setenv("MCP_RATE_READS_PER_MIN", "0")
        with admitted(resolver):
            result = call(api_client, "lem_get_generation_status")
        assert result["isError"] is True
        assert structured(result)["error"]["code"] == "read_budget_exhausted"


class TestLoopbackFor:
    def test_an_unadmitted_request_is_refused(self) -> None:
        with pytest.raises(ToolFailure):
            lb_mod.loopback_for(None)
        request = MagicMock()
        request.scope = {"state": {}}
        request.headers = {"authorization": f"Bearer {TOKEN}"}
        with pytest.raises(ToolFailure):
            lb_mod.loopback_for(request)


class TestTimeout:
    async def test_a_read_that_runs_out_of_time_is_timeout(self) -> None:
        async def _slow(_lb: Any, _args: Any) -> dict:
            await asyncio.sleep(1)
            return {}

        spec = calls.registry.TOOLS_BY_NAME["lem_get_generation_status"]
        request = MagicMock()
        request.scope = {"state": {"mcp_principal": _PRINCIPAL}}
        request.headers = {"authorization": f"Bearer {TOKEN}"}
        with patch.object(calls, "TOOL_TIMEOUT_SECONDS", 0.01), \
             patch.dict(calls.registry.TOOLS_BY_NAME,
                        {spec.name: spec.__class__(**{**spec.__dict__, "run": _slow})}), \
             patch("cqc_lem.api.main._client_ip", return_value=None), \
             patch.object(calls, "track_mcp_tool_call") as tracked:
            result = await calls.call_tool(request, spec.name, {})
        assert result.is_error is True
        assert result.structured_content["error"]["outcome"] == "timeout"
        assert tracked.call_args.args[2] == "timeout"

    async def test_an_unexpected_exception_is_upstream_error(self) -> None:
        async def _broken(_lb: Any, _args: Any) -> dict:
            raise KeyError("internal " + TOKEN)

        spec = calls.registry.TOOLS_BY_NAME["lem_get_generation_status"]
        request = MagicMock()
        request.scope = {"state": {"mcp_principal": _PRINCIPAL}}
        request.headers = {"authorization": f"Bearer {TOKEN}"}
        with patch.dict(calls.registry.TOOLS_BY_NAME,
                        {spec.name: spec.__class__(**{**spec.__dict__, "run": _broken})}), \
             patch("cqc_lem.api.main._client_ip", return_value=None), \
             patch.object(calls, "log_error") as logged:
            result = await calls.call_tool(request, spec.name, {})
        assert result.structured_content["error"]["outcome"] == "upstream_error"
        assert TOKEN not in str(result.content)
        assert logged.called
