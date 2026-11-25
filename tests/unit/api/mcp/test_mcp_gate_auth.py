"""`McpGate`, phase P2: steps 5 (revoke on exposure) and 7-13 (`docs/agent-mcp-plan.md` §3.3).

The P1 file (`test_mcp_gate.py`) pins steps 1-6 with a resolver that finds nothing. This one gives
the resolver rows, and follows an admitted request all the way to the SDK.
"""

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.api.mcp import gate as gate_mod, limits
from cqc_lem.platform.db.repositories.auth import SessionStoreUnavailable

from .conftest import FakeRedis

pytestmark = pytest.mark.unit

_JSON = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
_TOKEN = "lem_mcp_" + "gatecanary" * 3
_OTHER = "lem_mcp_" + "othercanary" * 3
_UID = 42
_EXPIRES = datetime(2099, 1, 1, tzinfo=timezone.utc)
_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
_CALL = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "lem_whoami", "arguments": {"secret_arg": "do-not-log"}}}


def _row(scope: str = "mcp", user_id: int = _UID) -> dict:
    return {"user_id": user_id, "scope": scope, "session_id": 900, "expires_at": _EXPIRES}


def _post(client: Any, body: Any = None, token: str = _TOKEN, path: str = "/api/mcp",
          **headers: str) -> Any:
    return client.post(path, content=json.dumps(_LIST if body is None else body),
                       headers={**_JSON, "Authorization": f"Bearer {token}", **headers})


def _reason(resp: Any) -> str:
    return resp.json().get("reason")


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rollout flag on, through its env fallback (PostHog flags are off in the unit lane)."""
    monkeypatch.setenv("MCP_SERVER_ENABLED", "true")


@pytest.fixture
def sdk() -> Iterator[MagicMock]:
    """Stand in for the SDK's session manager: record what reached it and answer 200."""
    seen: dict[str, Any] = {}

    async def _handle(scope: Any, receive: Any, send: Any) -> None:
        message = await receive()
        seen["body"] = message.get("body")
        seen["principal"] = scope["state"]["mcp_principal"]
        seen["in_flight"] = limits.worker_slots.in_flight
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"ok": true}'})

    fake = MagicMock()
    fake.session_manager.handle_request = _handle
    with patch.object(gate_mod, "mcp_server", fake):
        fake.seen = seen
        yield fake


@pytest.fixture(autouse=True)
def _quiet_access() -> Iterator[MagicMock]:
    """The daily access row: recorded through `_record_daily_access`, captured here."""
    with patch("cqc_lem.api.main._safe_auth_event") as audit, \
         patch("cqc_lem.api.main.get_session_id", return_value=900):
        yield audit


class TestResolve:
    @pytest.mark.parametrize("scope", ["mcp", "mcp_outreach"])
    def test_an_mcp_session_is_admitted_and_reaches_the_sdk(
            self, api_client: Any, fake_redis: FakeRedis, resolver: MagicMock, enabled: None,
            sdk: MagicMock, scope: str) -> None:
        resolver.return_value = _row(scope)
        resp = _post(api_client, _CALL, **{"User-Agent": "claude-code/2.1 (cli)"})
        assert resp.status_code == 200, resp.text
        principal = sdk.seen["principal"]
        assert (principal.user_id, principal.session_id, principal.scope) == (_UID, 900, scope)
        assert principal.expires_at == _EXPIRES
        assert principal.client_name == "claude-code/2.1 (cli)"
        assert json.loads(sdk.seen["body"]) == _CALL, "the SDK gets the body the gate already read"
        assert _TOKEN not in repr(principal)
        assert limits.worker_slots.in_flight == 0, "the slot is released after the SDK answers"
        assert fake_redis.store["mcp:inflight:42"] == 0

    @pytest.mark.parametrize("scope", ["full", "recovery", "agent", "observer", "operator",
                                       "assist", "extension", "enroll"])
    def test_any_other_scope_is_the_same_401_and_warns(
            self, api_client: Any, fake_redis: FakeRedis, resolver: MagicMock, enabled: None,
            sdk: MagicMock, scope: str, caplog: pytest.LogCaptureFixture) -> None:
        resolver.return_value = _row(scope)
        with caplog.at_level(logging.DEBUG, logger="cqc-lem"):
            resp = _post(api_client)
        assert resp.status_code == 401
        assert _reason(resp) == "invalid_token"
        assert "resource_metadata" not in resp.headers["www-authenticate"]
        warned = [r for r in caplog.records if "not an MCP session" in r.getMessage()]
        assert [r.levelno for r in warned] == [logging.WARNING]
        assert "body" not in sdk.seen

    def test_unknown_expired_or_revoked_is_401_at_debug(
            self, api_client: Any, fake_redis: FakeRedis, resolver: MagicMock,
            caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG, logger="cqc-lem"):
            resp = _post(api_client)
        assert resp.status_code == 401 and _reason(resp) == "invalid_token"
        assert "Agents" in resp.json()["detail"]
        refused = [r for r in caplog.records if "does not resolve" in r.getMessage()]
        assert [r.levelno for r in refused] == [logging.DEBUG]

    def test_a_database_fault_is_503_not_401(self, api_client: Any, fake_redis: FakeRedis,
                                             resolver: MagicMock) -> None:
        resolver.side_effect = SessionStoreUnavailable()
        resp = _post(api_client)
        assert resp.status_code == 503 and _reason(resp) == "mcp_unavailable"

    def test_the_flag_off_is_403(self, api_client: Any, fake_redis: FakeRedis,
                                 resolver: MagicMock, sdk: MagicMock) -> None:
        resolver.return_value = _row()
        resp = _post(api_client)
        assert resp.status_code == 403 and _reason(resp) == "mcp_disabled"
        assert "body" not in sdk.seen

    def test_the_flag_is_asked_about_the_tokens_user(self, api_client: Any, fake_redis: FakeRedis,
                                                     resolver: MagicMock, sdk: MagicMock) -> None:
        resolver.return_value = _row()
        with patch.object(gate_mod, "flag_enabled", return_value=True) as flag:
            assert _post(api_client).status_code == 200
        flag.assert_called_once_with(gate_mod.MCP_SERVER, _UID)


class TestFailureBucket:
    def test_thirty_failures_a_minute_then_429(self, api_client: Any, fake_redis: FakeRedis,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(limits.ENV_FAILURES_PER_MIN, "3")
        codes = [_post(api_client).status_code for _ in range(4)]
        assert codes == [401, 401, 401, 429]
        resp = _post(api_client)
        assert _reason(resp) == "rate_limited" and resp.headers["retry-after"] == "60"

    def test_a_bad_token_on_an_address_does_not_lock_out_a_good_one(
            self, api_client: Any, fake_redis: FakeRedis, resolver: MagicMock, enabled: None,
            sdk: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(limits.ENV_FAILURES_PER_MIN, "2")
        resolver.side_effect = lambda t, **_: _row() if t == _TOKEN else None
        ip = {"CF-Connecting-IP": "203.0.113.7"}
        assert [_post(api_client, token=_OTHER, **ip).status_code for _ in range(3)] == [
            401, 401, 429]
        assert _post(api_client, **ip).status_code == 200

    def test_a_wrong_scope_counts_as_a_failure(self, api_client: Any, fake_redis: FakeRedis,
                                               resolver: MagicMock,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(limits.ENV_FAILURES_PER_MIN, "1")
        resolver.return_value = _row("full")
        assert [_post(api_client).status_code for _ in range(2)] == [401, 429]

    def test_an_unreadable_bucket_is_503(self, api_client: Any, fake_redis: FakeRedis) -> None:
        with patch.object(gate_mod, "auth_failures_exceeded",
                          side_effect=limits.LimitsUnavailable()):
            resp = _post(api_client)
        assert resp.status_code == 503 and _reason(resp) == "mcp_unavailable"


class TestConcurrencyAndBudgets:
    @pytest.fixture(autouse=True)
    def _admitted(self, resolver: MagicMock, enabled: None) -> None:
        resolver.return_value = _row()

    def test_a_user_at_two_in_flight_gets_a_busy_tool_result_not_429(
            self, api_client: Any, fake_redis: FakeRedis, sdk: MagicMock) -> None:
        fake_redis.store["mcp:inflight:42"] = 2
        resp = _post(api_client, _CALL)
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == 2 and body["result"]["isError"] is True
        assert "Retry" in body["result"]["content"][0]["text"]
        assert "body" not in sdk.seen, "a busy call never reaches the SDK"
        assert fake_redis.store["mcp:inflight:42"] == 2, "the refused slot is handed back"
        assert "mcp:inflight:42" not in fake_redis.ttl_writes, "a refusal never touches the TTL"
        assert limits.worker_slots.in_flight == 0

    def test_a_busy_batch_with_a_non_call_keeps_the_429(
            self, api_client: Any, fake_redis: FakeRedis, sdk: MagicMock) -> None:
        fake_redis.store["mcp:inflight:42"] = 2
        resp = _post(api_client, [_LIST, _CALL])
        assert resp.status_code == 429 and _reason(resp) == "concurrency_limited"

    def test_six_concurrent_discovery_requests_all_succeed_at_the_cap(
            self, api_client: Any, fake_redis: FakeRedis, sdk: MagicMock) -> None:
        fake_redis.store["mcp:inflight:42"] = 2
        methods = ["initialize", "tools/list", "prompts/list", "resources/list",
                   "resources/templates/list", "ping"]
        with ThreadPoolExecutor(max_workers=6) as pool:
            codes = list(pool.map(
                lambda m: _post(api_client, {"jsonrpc": "2.0", "id": 1, "method": m}).status_code,
                methods))
        assert codes == [200] * 6
        assert fake_redis.store["mcp:inflight:42"] == 2, "discovery never touches the user slot"

    def test_a_user_slot_still_bounds_tool_calls_with_429_when_not_answerable(
            self, api_client: Any, fake_redis: FakeRedis, sdk: MagicMock) -> None:
        fake_redis.store["mcp:inflight:42"] = 2
        resp = _post(api_client, {"jsonrpc": "2.0", "method": "tools/call",
                                  "params": {"name": "lem_whoami"}})
        assert resp.status_code == 429 and _reason(resp) == "concurrency_limited"
        assert resp.headers["retry-after"] == "1"
        assert fake_redis.store["mcp:inflight:42"] == 2, "the refused slot is handed back"
        assert "mcp:inflight:42" not in fake_redis.ttl_writes, "a refusal never touches the TTL"
        assert limits.worker_slots.in_flight == 0

    def test_a_full_worker_gets_429_without_touching_redis_slots(
            self, api_client: Any, fake_redis: FakeRedis, sdk: MagicMock,
            monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(limits.ENV_INFLIGHT_PER_WORKER, "0")
        resp = _post(api_client)  # discovery is exempt from the user slot, never the worker cap
        assert resp.status_code == 429 and _reason(resp) == "concurrency_limited"
        assert "mcp:inflight:42" not in fake_redis.store

    def test_each_tool_call_spends_the_budget(self, api_client: Any, fake_redis: FakeRedis,
                                              sdk: MagicMock,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(limits.ENV_TOOL_CALLS_PER_MIN, "2")
        codes = [_post(api_client, _CALL).status_code for _ in range(3)]
        assert codes == [200, 200, 429]
        assert _post(api_client, _LIST).status_code == 200, "a non-tool call spends nothing"

    def test_a_batch_spends_one_unit_per_tool_call(self, api_client: Any, fake_redis: FakeRedis,
                                                   sdk: MagicMock,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(limits.ENV_TOOL_CALLS_PER_MIN, "2")
        assert _post(api_client, [_CALL, _CALL, _CALL]).status_code == 429

    def test_a_zero_budget_refuses_every_tool_call(self, api_client: Any, fake_redis: FakeRedis,
                                                   sdk: MagicMock,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(limits.ENV_TOOL_CALLS_PER_MIN, "0")
        resp = _post(api_client, _CALL)
        assert resp.status_code == 429 and _reason(resp) == "rate_limited"
        assert int(resp.headers["retry-after"]) >= 1
        assert fake_redis.store["mcp:inflight:42"] == 0
        assert limits.worker_slots.in_flight == 0

    def test_a_redis_fault_in_the_limits_is_503_and_releases_the_worker_slot(
            self, api_client: Any, fake_redis: FakeRedis, sdk: MagicMock) -> None:
        with patch.object(gate_mod, "acquire_user_slot", side_effect=limits.LimitsUnavailable()):
            resp = _post(api_client, _CALL)
        assert resp.status_code == 503 and _reason(resp) == "mcp_unavailable"
        assert limits.worker_slots.in_flight == 0

    def test_a_raising_sdk_still_releases_both_slots(self, api_client: Any,
                                                     fake_redis: FakeRedis) -> None:
        async def _boom(scope: Any, receive: Any, send: Any) -> None:
            raise RuntimeError("sdk fell over")

        fake = MagicMock()
        fake.session_manager.handle_request = _boom
        with patch.object(gate_mod, "mcp_server", fake):
            assert _post(api_client, _CALL).status_code == 500
        assert limits.worker_slots.in_flight == 0
        assert fake_redis.store["mcp:inflight:42"] == 0


class TestAccessRecord:
    def test_one_access_row_a_day_with_the_tool_and_never_the_arguments(
            self, api_client: Any, fake_redis: FakeRedis, resolver: MagicMock, enabled: None,
            sdk: MagicMock, _quiet_access: MagicMock) -> None:
        resolver.return_value = _row()
        with patch("cqc_lem.utilities.observer_access.shared_redis_client",
                   return_value=fake_redis):
            for _ in range(3):
                assert _post(api_client, _CALL).status_code == 200
        rows = [c for c in _quiet_access.call_args_list
                if c.args and str(c.args[0]) == "mcp_access"]
        assert len(rows) == 1
        details = rows[0].kwargs["details"]
        assert details["scope"] == "mcp" and details["path"] == "/api/mcp"
        assert details["tool"] == "lem_whoami"
        assert "do-not-log" not in json.dumps(details)
        assert rows[0].kwargs["session_id"] == 900

    def test_tool_calls_are_counted_for_the_agents_tab(
            self, api_client: Any, fake_redis: FakeRedis, resolver: MagicMock, enabled: None,
            sdk: MagicMock) -> None:
        resolver.return_value = _row()
        _post(api_client, _CALL, **{"User-Agent": "codex"})
        _post(api_client, _LIST, **{"User-Agent": "codex"})
        activity = limits.read_activity([900])
        assert activity == {900: {"calls_7d": 1, "last_client_name": "codex"}}


class TestClientName:
    def test_initialize_client_info_wins_over_the_user_agent(self) -> None:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"clientInfo": {"name": "Cursor\n<script>", "version": "1"}}})
        summary = gate_mod._rpc_summary(body.encode())
        from starlette.datastructures import Headers

        assert gate_mod._client_name(summary, Headers({"user-agent": "ua"})) == "Cursorscript"

    def test_a_long_user_agent_is_cut_to_forty(self) -> None:
        from starlette.datastructures import Headers

        name = gate_mod._client_name(gate_mod.RpcSummary(), Headers({"user-agent": "x" * 90}))
        assert name == "x" * 40

    @pytest.mark.parametrize("body", [b"", b"not json", b"[1, 2]", b'"str"', b"\xff\xfe",
                                      b'{"method": 7}', b'{"method": "tools/call", "params": 3}'])
    def test_any_body_is_a_safe_summary(self, body: bytes) -> None:
        summary = gate_mod._rpc_summary(body)
        assert summary.client_info_name is None and summary.first_tool is None

    def test_a_batch_counts_every_tool_call(self) -> None:
        summary = gate_mod._rpc_summary(json.dumps([_CALL, _LIST, _CALL]).encode())
        assert (summary.tool_calls, summary.first_tool, summary.first_method) == (
            2, "lem_whoami", "tools/call")


class TestQueryTokenExposure:
    @pytest.mark.parametrize("param", ["token", "session_token", "access_token"])
    def test_a_resolving_query_token_is_revoked_audited_and_emailed(
            self, api_client: Any, fake_redis: FakeRedis, exposure: MagicMock, param: str,
            caplog: pytest.LogCaptureFixture) -> None:
        exposure.side_effect = lambda v: ({"user_id": 7, "session_id": 31, "scope": "full",
                                           "label": "Chrome on macOS"} if v == _TOKEN else None)
        with patch.object(gate_mod, "record_auth_event") as audit, \
             patch("cqc_lem.api.mcp.notify.notify_token_event") as notify, \
             caplog.at_level(logging.DEBUG, logger="cqc-lem"):
            resp = api_client.post(f"/api/mcp?{param}={_TOKEN}", content=b"{}", headers=_JSON)
        assert resp.status_code == 400 and _reason(resp) == "token_in_url_refused"
        exposure.assert_called_once_with(_TOKEN)
        assert str(audit.call_args.args[0]) == "mcp_token_exposed"
        assert audit.call_args.kwargs["user_id"] == 7
        assert audit.call_args.kwargs["session_id"] == 31
        assert audit.call_args.kwargs["details"] == {"scope": "full", "param": param}
        notify.assert_called_once_with(7, "exposed", label="Chrome on macOS", scope="full")
        warned = [r for r in caplog.records if "was revoked" in r.getMessage()]
        assert [r.levelno for r in warned] == [logging.WARNING]
        for record in caplog.records:
            if record.name.startswith("httpx"):
                continue
            assert _TOKEN not in record.getMessage() and _TOKEN not in str(record.__dict__)

    def test_an_unresolved_query_token_is_info_and_revokes_nothing(
            self, api_client: Any, fake_redis: FakeRedis, exposure: MagicMock,
            caplog: pytest.LogCaptureFixture) -> None:
        with patch.object(gate_mod, "record_auth_event") as audit, \
             caplog.at_level(logging.DEBUG, logger="cqc-lem"):
            resp = api_client.post("/api/mcp?token=nothing-here", content=b"{}", headers=_JSON)
        assert resp.status_code == 400
        audit.assert_not_called()
        assert [r.levelno for r in caplog.records if "query string" in r.getMessage()] == [
            logging.INFO]

    def test_at_most_four_values_are_looked_up(self, api_client: Any, fake_redis: FakeRedis,
                                               exposure: MagicMock) -> None:
        query = "&".join(f"token=v{i}" for i in range(10))
        assert api_client.post(f"/api/mcp?{query}", content=b"{}",
                               headers=_JSON).status_code == 400
        assert exposure.call_count == 4

    def test_an_empty_value_is_not_looked_up(self, api_client: Any, fake_redis: FakeRedis,
                                             exposure: MagicMock) -> None:
        assert api_client.post("/api/mcp?token=", content=b"{}", headers=_JSON).status_code == 400
        exposure.assert_not_called()


class TestTheTokenIsNeverLogged:
    def test_no_record_on_any_path_carries_the_token(
            self, api_client: Any, fake_redis: FakeRedis, resolver: MagicMock, enabled: None,
            sdk: MagicMock, caplog: pytest.LogCaptureFixture,
            monkeypatch: pytest.MonkeyPatch) -> None:
        with caplog.at_level(logging.DEBUG, logger="cqc-lem"):
            _post(api_client)                                  # unresolved
            resolver.return_value = _row("full")
            _post(api_client)                                  # wrong scope (WARNING)
            resolver.return_value = _row()
            _post(api_client, _CALL)                           # admitted
            monkeypatch.setenv(limits.ENV_TOOL_CALLS_PER_MIN, "0")
            _post(api_client, _CALL)                           # budget
            resolver.side_effect = SessionStoreUnavailable()
            _post(api_client)                                  # store fault
        kept = [r for r in caplog.records if not r.name.startswith("httpx")]
        assert kept
        for record in kept:
            assert _TOKEN not in record.getMessage()
            assert _TOKEN not in str(record.__dict__)
            assert "do-not-log" not in str(record.__dict__)


class TestTheRealSdk:
    def test_initialize_through_the_real_session_manager(
            self, api_client: Any, fake_redis: FakeRedis, resolver: MagicMock,
            enabled: None) -> None:
        """No stub: the gate hands an admitted request to the SDK, which answers JSON-RPC."""
        resolver.return_value = _row()
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "pytest", "version": "0"}}}
        resp = _post(api_client, body)
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["id"] == 1 and payload["result"]["serverInfo"]["name"] == "lem"
        assert "no-store" in resp.headers["cache-control"]


class TestTheGateWritesNothingForANonMcpSession:
    def test_the_lookup_may_write_only_for_the_mcp_scopes(self, api_client: Any,
                                                          fake_redis: FakeRedis,
                                                          resolver: MagicMock) -> None:
        resolver.return_value = _row("full")
        assert _post(api_client).status_code == 401
        assert resolver.call_args.args == (_TOKEN,)
        assert resolver.call_args.kwargs == {"mutate_scopes": frozenset({"mcp", "mcp_outreach"})}


class TestThePerAddressBucket:
    def test_a_random_token_flood_is_refused_before_the_database(
            self, api_client: Any, fake_redis: FakeRedis, resolver: MagicMock,
            monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(limits.ENV_ADDRESS_FAILURES_PER_MIN, "3")
        ip = {"CF-Connecting-IP": "198.51.100.4"}
        codes = [_post(api_client, token=f"lem_mcp_random{i}", **ip).status_code for i in range(6)]
        assert codes == [401, 401, 401, 429, 429, 429]
        assert resolver.call_count == 3, "the flood stopped reaching the session lookup"

    def test_another_address_is_not_held_by_the_flood(
            self, api_client: Any, fake_redis: FakeRedis, resolver: MagicMock, enabled: None,
            sdk: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(limits.ENV_ADDRESS_FAILURES_PER_MIN, "1")
        resolver.side_effect = lambda t, **_: _row() if t == _TOKEN else None
        _post(api_client, token=_OTHER, **{"CF-Connecting-IP": "198.51.100.4"})
        assert _post(api_client, token=_OTHER,
                     **{"CF-Connecting-IP": "198.51.100.4"}).status_code == 429
        assert _post(api_client, **{"CF-Connecting-IP": "203.0.113.50"}).status_code == 200

    def test_query_token_lookups_run_under_the_address_bucket(
            self, api_client: Any, fake_redis: FakeRedis, exposure: MagicMock,
            monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(limits.ENV_ADDRESS_FAILURES_PER_MIN, "5")
        codes = [api_client.post("/api/mcp?" + "&".join(f"token=v{r}{i}" for i in range(4)),
                                 content=b"{}", headers=_JSON).status_code for r in range(3)]
        assert codes == [400, 400, 429]
        assert exposure.call_count == 8, "four lookups each for two requests, none for the third"

    @pytest.mark.parametrize("path", ["/api/mcp", "/api/mcp?token=x"])
    def test_an_unreadable_address_bucket_is_503(self, api_client: Any, fake_redis: FakeRedis,
                                                 exposure: MagicMock, path: str) -> None:
        with patch.object(gate_mod, "address_failures_exceeded",
                          side_effect=limits.LimitsUnavailable()):
            resp = _post(api_client, path=path)
        assert resp.status_code == 503 and _reason(resp) == "mcp_unavailable"
        exposure.assert_not_called()


class TestSlotsAreReturnedOnEveryExit:
    @pytest.fixture(autouse=True)
    def _admitted(self, resolver: MagicMock, enabled: None) -> None:
        resolver.return_value = _row()

    @pytest.mark.parametrize("target", ["acquire_user_slot", "consume_budgets", "_record_access"])
    def test_any_exception_after_the_worker_slot_returns_it(
            self, api_client: Any, fake_redis: FakeRedis, sdk: MagicMock, target: str) -> None:
        with patch.object(gate_mod, target, side_effect=RuntimeError("boom")):
            assert _post(api_client, _CALL).status_code == 500
        assert limits.worker_slots.in_flight == 0
        assert int(fake_redis.store.get("mcp:inflight:42") or 0) == 0
        assert "body" not in sdk.seen

    def _scope(self) -> dict:
        return {"type": "http", "method": "POST", "path": "/api/mcp", "query_string": b"",
                "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                            (b"authorization", f"Bearer {_TOKEN}".encode())]}

    @pytest.fixture
    def gate_up(self, fake_redis: FakeRedis) -> Iterator[None]:
        from cqc_lem.api.mcp.lifespan import mcp_state

        saved = (mcp_state.unavailable, mcp_state.host, mcp_state.origin, mcp_state.dev_loopback)
        mcp_state.unavailable, mcp_state.host, mcp_state.origin, mcp_state.dev_loopback = (
            False, "testserver", "http://testserver", False)
        yield
        (mcp_state.unavailable, mcp_state.host, mcp_state.origin,
         mcp_state.dev_loopback) = saved

    def test_a_cancelled_request_returns_both_slots(self, fake_redis: FakeRedis,
                                                    gate_up: None) -> None:
        """Cancelled mid-SDK the way Starlette cancels a request: an anyio cancel scope."""
        import anyio

        held: dict[str, Any] = {}

        async def _drive() -> None:
            started = anyio.Event()

            async def _hang(scope: Any, receive: Any, send: Any) -> None:
                held["in_flight"] = limits.worker_slots.in_flight
                held["user"] = fake_redis.store.get("mcp:inflight:42")
                started.set()
                await anyio.sleep_forever()

            async def _receive() -> dict:
                return {"type": "http.request", "body": json.dumps(_CALL).encode()}

            async def _send(message: dict) -> None:
                return None

            fake = MagicMock()
            fake.session_manager.handle_request = _hang
            with patch.object(gate_mod, "mcp_server", fake):
                async with anyio.create_task_group() as tg:
                    tg.start_soon(gate_mod.McpGate(self._scope(), _receive, _send).dispatch)
                    await started.wait()
                    tg.cancel_scope.cancel()

        anyio.run(_drive)
        assert held == {"in_flight": 1, "user": 1}, "both slots were held while the SDK ran"
        assert limits.worker_slots.in_flight == 0
        assert fake_redis.store["mcp:inflight:42"] == 0

    def test_a_natively_cancelled_task_returns_both_slots(self, fake_redis: FakeRedis,
                                                          gate_up: None) -> None:
        held: dict[str, Any] = {}

        async def _drive() -> None:
            started = asyncio.Event()

            async def _hang(scope: Any, receive: Any, send: Any) -> None:
                held["in_flight"] = limits.worker_slots.in_flight
                started.set()
                await asyncio.Event().wait()

            async def _receive() -> dict:
                return {"type": "http.request", "body": json.dumps(_CALL).encode()}

            async def _send(message: dict) -> None:
                return None

            fake = MagicMock()
            fake.session_manager.handle_request = _hang
            with patch.object(gate_mod, "mcp_server", fake):
                task = asyncio.ensure_future(
                    gate_mod.McpGate(self._scope(), _receive, _send).dispatch())
                await started.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        asyncio.run(_drive())
        assert held == {"in_flight": 1}
        assert limits.worker_slots.in_flight == 0
        assert fake_redis.store["mcp:inflight:42"] == 0


    def test_a_request_cancelled_while_taking_its_slots_returns_them(
            self, fake_redis: FakeRedis, gate_up: None) -> None:
        """The window the old code leaked in: worker slot taken, budget still being spent."""
        import threading

        import anyio

        in_budget = threading.Event()
        let_go = threading.Event()
        held: dict[str, Any] = {}

        def _slow_budget(user_id: int, budgets: Any) -> None:
            held["in_flight"] = limits.worker_slots.in_flight
            in_budget.set()
            let_go.wait(5)
            return None

        async def _drive() -> None:
            async def _receive() -> dict:
                return {"type": "http.request", "body": json.dumps(_CALL).encode()}

            async def _send(message: dict) -> None:
                return None

            sdk_called = MagicMock()
            fake = MagicMock()
            fake.session_manager.handle_request = sdk_called
            with patch.object(gate_mod, "mcp_server", fake), \
                 patch.object(gate_mod, "consume_budgets", _slow_budget):
                async with anyio.create_task_group() as tg:
                    tg.start_soon(gate_mod.McpGate(self._scope(), _receive, _send).dispatch)
                    await anyio.to_thread.run_sync(in_budget.wait, 5)
                    tg.cancel_scope.cancel()
                    let_go.set()
            held["sdk_called"] = sdk_called.called

        anyio.run(_drive)
        assert held == {"in_flight": 1, "sdk_called": False}
        assert limits.worker_slots.in_flight == 0
        assert fake_redis.store["mcp:inflight:42"] == 0

class TestCountersCarryTheirTtlFromCreation:
    KEY = "mcp:inflight:7"

    def test_the_ttl_is_set_once_and_never_by_a_refused_attempt(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(limits.ENV_INFLIGHT_PER_USER, "2")
        fake = FakeRedis()
        with patch.object(limits, "_redis", return_value=fake):
            assert [limits.acquire_user_slot(7) for _ in range(4)] == [True, True, False, False]
        assert fake.ttl_writes == [self.KEY], "only the creating call set a TTL"
        assert fake.expiries[self.KEY] == limits._INFLIGHT_TTL_SECONDS
        assert fake.store[self.KEY] == 2

    def test_a_failed_increment_leaves_a_counter_that_still_has_its_ttl(self) -> None:
        fake = FakeRedis()
        fake.fail_on = {"incr"}
        with patch.object(limits, "_redis", return_value=fake), \
             pytest.raises(limits.LimitsUnavailable):
            limits.acquire_user_slot(7)
        assert fake.store[self.KEY] == 0 and self.KEY in fake.expiries

    def test_a_failed_create_leaves_no_counter(self) -> None:
        fake = FakeRedis()
        fake.fail_on = {"set"}
        with patch.object(limits, "_redis", return_value=fake), \
             pytest.raises(limits.LimitsUnavailable):
            limits.acquire_user_slot(7)
        assert self.KEY not in fake.store

    def test_an_existing_counter_is_never_given_a_new_ttl(self) -> None:
        fake = FakeRedis()
        fake.store["k"] = 5
        assert limits._incr_with_ttl(fake, "k", 30) == 6
        assert fake.ttl_writes == []

    def test_every_counter_goes_through_the_helper(self) -> None:
        import inspect

        source = inspect.getsource(limits)
        assert source.count(".incr(") == 1, "a bare INCR elsewhere could create a TTL-less key"
        assert ".expire(" not in source

    def test_a_failure_counts_against_both_buckets(self) -> None:
        fake = FakeRedis()
        with patch.object(limits, "_redis", return_value=fake):
            limits.record_auth_failure("ip", "tok")
            limits.record_auth_failure("ip", None)
        assert fake.store[limits._address_failure_key("ip")] == 2
        assert fake.store[limits._failure_key("ip", "tok")] == 1


class TestLimitsUnit:
    def test_limit_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for raw, expected in (("", 60), ("abc", 60), ("-3", 60), ("0", 0), ("17", 17)):
            monkeypatch.setenv(limits.ENV_TOOL_CALLS_PER_MIN, raw)
            assert limits.limit(limits.ENV_TOOL_CALLS_PER_MIN) == expected

    def test_budgets_without_redis_fail_closed(self) -> None:
        with patch.object(limits, "_redis", return_value=None), \
             pytest.raises(limits.LimitsUnavailable):
            limits.consume_budgets(1, limits.READ_BUDGETS)

    def test_a_raising_redis_fails_the_budget_closed(self) -> None:
        with patch.object(limits, "_redis", return_value=FakeRedis(raises=True)), \
             pytest.raises(limits.LimitsUnavailable):
            limits.consume_budgets(1, limits.WRITE_BUDGETS)

    def test_the_read_budget_refuses_past_either_window(self,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(limits.ENV_READS_PER_DAY, "2")
        fake = FakeRedis()
        with patch.object(limits, "_redis", return_value=fake):
            results = [limits.consume_budgets(5, limits.READ_BUDGETS) for _ in range(3)]
        assert results[:2] == [None, None] and results[2] >= 1

    def test_the_failure_bucket_fails_closed(self) -> None:
        with patch.object(limits, "_redis", return_value=None), \
             pytest.raises(limits.LimitsUnavailable):
            limits.auth_failures_exceeded("1.2.3.4", "t")
        with patch.object(limits, "_redis", return_value=FakeRedis(raises=True)), \
             pytest.raises(limits.LimitsUnavailable):
            limits.auth_failures_exceeded("1.2.3.4", "t")

    def test_a_zero_failure_limit_refuses_and_a_garbled_count_refuses(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeRedis()
        with patch.object(limits, "_redis", return_value=fake):
            monkeypatch.setenv(limits.ENV_FAILURES_PER_MIN, "0")
            assert limits.auth_failures_exceeded("ip", "t") is True
            monkeypatch.setenv(limits.ENV_FAILURES_PER_MIN, "5")
            fake.store[limits._failure_key("ip", "t")] = "garbled"
            assert limits.auth_failures_exceeded("ip", "t") is True

    def test_the_key_stores_neither_the_address_nor_the_token(self) -> None:
        key = limits._failure_key("203.0.113.9", _TOKEN)
        assert "203.0.113.9" not in key and _TOKEN not in key

    def test_best_effort_writers_never_raise(self) -> None:
        for client in (None, FakeRedis(raises=True)):
            with patch.object(limits, "_redis", return_value=client):
                limits.record_auth_failure("ip", "t")
                limits.release_user_slot(1)
                limits.record_activity(9, "codex", True)
        with patch.object(limits, "_redis", return_value=FakeRedis()):
            limits.record_activity(None, "codex", True)

    def test_a_user_slot_with_zero_cap_or_bad_redis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(limits.ENV_INFLIGHT_PER_USER, "0")
        assert limits.acquire_user_slot(1) is False
        monkeypatch.setenv(limits.ENV_INFLIGHT_PER_USER, "2")
        with patch.object(limits, "_redis", return_value=FakeRedis(raises=True)), \
             pytest.raises(limits.LimitsUnavailable):
            limits.acquire_user_slot(1)

    def test_a_negative_slot_count_is_cleared(self) -> None:
        fake = FakeRedis()
        with patch.object(limits, "_redis", return_value=fake):
            limits.release_user_slot(3)
        assert "mcp:inflight:3" not in fake.store

    def test_activity_is_unknown_never_zero_when_unreadable(self) -> None:
        with patch.object(limits, "_redis", return_value=None):
            assert limits.read_activity([1]) is None
        with patch.object(limits, "_redis", return_value=FakeRedis(raises=True)):
            assert limits.read_activity([1]) is None

    def test_activity_sums_seven_days_and_decodes_the_name(self) -> None:
        fake = FakeRedis()
        fake.store[f"mcp:calls:4:{limits._day(0)}"] = b"2"
        fake.store[f"mcp:calls:4:{limits._day(6)}"] = b"3"
        fake.store[f"mcp:calls:4:{limits._day(7)}"] = b"100"
        fake.store["mcp:client:4"] = b"Cursor"
        with patch.object(limits, "_redis", return_value=fake):
            assert limits.read_activity([4]) == {4: {"calls_7d": 5, "last_client_name": "Cursor"}}

    def test_the_mint_limiter_fails_open_and_caps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for client in (None, FakeRedis(raises=True)):
            with patch.object(limits, "_redis", return_value=client):
                assert limits.claim_mint(1) is True
        monkeypatch.setenv(limits.ENV_MINTS_PER_HOUR, "2")
        with patch.object(limits, "_redis", return_value=FakeRedis()):
            assert [limits.claim_mint(1) for _ in range(3)] == [True, True, False]
        monkeypatch.setenv(limits.ENV_MINTS_PER_HOUR, "0")
        assert limits.claim_mint(1) is False

    def test_the_kill_key_setter(self) -> None:
        fake = FakeRedis()
        with patch.object(limits, "_redis", return_value=fake):
            assert limits.set_kill_key(True) is True
            assert limits.read_kill_key() is limits.KillState.ON
            assert limits.set_kill_key(False) is True
            assert limits.read_kill_key() is limits.KillState.OFF
        with patch.object(limits, "_redis", return_value=None):
            assert limits.set_kill_key(True) is False
        with patch.object(limits, "_redis", return_value=FakeRedis(raises=True)):
            assert limits.set_kill_key(True) is False

    def test_worker_slots_never_go_negative(self) -> None:
        slots = limits.WorkerSlots()
        slots.release()
        assert slots.in_flight == 0
        assert slots.try_acquire() is True and slots.in_flight == 1


def test_expires_at_is_a_real_datetime_not_a_string() -> None:
    """Guard the fixture itself: a string here would let a type bug through the principal tests."""
    assert isinstance(_row()["expires_at"], datetime)
    assert _row()["expires_at"] - timedelta(days=1) < _EXPIRES
