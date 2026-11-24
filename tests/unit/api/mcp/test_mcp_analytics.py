"""MCP telemetry: the two EventSpecs through `_emit`, and `posthog.mcp` with NO content (owner ruling O8).

The contract under test: no tool argument and no tool result reaches ANY PostHog capture, neither
LEM's own `_emit` (`posthog.capture`) nor the `posthog.mcp` client's `capture_tool_call`. Both are
replaced with recorders and a tool is called with canary values in its arguments and its result.
"""

import json
import logging
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities import observability

from ._admitted import TOKEN, admitted, call, rpc

pytestmark = pytest.mark.unit

_ARG_CANARY = "argcanary7731"
_RESULT_CANARY = "resultcanary5512"
_O = "cqc_lem.utilities.observability"


@pytest.fixture(autouse=True)
def enabled(monkeypatch: pytest.MonkeyPatch, fake_redis: Any) -> None:
    monkeypatch.setenv("MCP_SERVER_ENABLED", "true")


@pytest.fixture
def recorders() -> Iterator[tuple[MagicMock, MagicMock]]:
    """`posthog.capture` (behind `_emit`) and the `posthog.mcp` client, both recording."""
    mcp_client = MagicMock()
    with patch(f"{_O}.posthog") as ph, patch(f"{_O}.telemetry_muted", return_value=False), \
         patch(f"{_O}._mcp_analytics", return_value=mcp_client):
        yield ph, mcp_client


def _everything_sent(ph: MagicMock, mcp_client: MagicMock) -> str:
    return json.dumps([repr(c) for c in ph.mock_calls + mcp_client.mock_calls])


class TestNoContentReachesPostHog:
    def test_arguments_and_results_never_reach_any_capture(
            self, api_client: Any, resolver: MagicMock,
            recorders: tuple[MagicMock, MagicMock]) -> None:
        ph, mcp_client = recorders
        with admitted(resolver), \
             patch("cqc_lem.api.main.get_recent_logs",
                   return_value=[{"id": 1, "action_type": "comment", "result": "success",
                                  "post_id": None, "post_url": None,
                                  "message": _RESULT_CANARY, "created_at": None}]):
            ok = call(api_client, "lem_get_activity", {"limit": 3, "include_text": True})
            bad = call(api_client, "lem_get_activity", {"limit": _ARG_CANARY})
        assert _RESULT_CANARY in json.dumps(ok), "the canary did reach the result"
        assert bad["isError"] is True
        sent = _everything_sent(ph, mcp_client)
        for canary in (_ARG_CANARY, _RESULT_CANARY, TOKEN):
            assert canary not in sent

        captures = mcp_client.capture_tool_call.call_args_list
        assert len(captures) == 2
        for capture in captures:
            assert capture.args == ("lem_get_activity",)
            assert capture.kwargs["parameters"] is None
            assert capture.kwargs["response"] is None
            assert capture.kwargs["error"] is None
            assert capture.kwargs["distinct_id"] == "77"
        assert captures[1].kwargs["is_error"] is True
        assert captures[1].kwargs["error_type"] == "invalid_args"

    def test_mcp_tool_call_is_emitted_through_emit_with_string_labels(
            self, api_client: Any, resolver: MagicMock,
            recorders: tuple[MagicMock, MagicMock]) -> None:
        ph, _ = recorders
        with admitted(resolver), patch("cqc_lem.api.main.get_generation_status", return_value=None):
            call(api_client, "lem_get_generation_status")
        events = [c.kwargs for c in ph.capture.call_args_list if c.kwargs["event"] == "mcp_tool_call"]
        assert len(events) == 1
        props = events[0]["properties"]
        assert events[0]["distinct_id"] == "77"
        assert props["tool"] == "lem_get_generation_status" and props["outcome"] == "ok"
        assert props["scope"] == "mcp" and props["inner_calls"] == 1 and props["inner_status"] == 200
        for name in ("tool", "outcome", "client_name", "scope"):
            assert isinstance(props[name], str)
        inner = [c.kwargs for c in ph.capture.call_args_list if c.kwargs["event"] == "api_call"]
        assert any(c["properties"]["route"].startswith("/api/content_generation_status")
                   for c in inner), "the inner route keeps its own api_call"


class TestAuthRefused:
    @pytest.mark.parametrize("token, row, reason", [
        ("", None, "missing"), (TOKEN, None, "invalid"),
        (TOKEN, {"user_id": 1, "scope": "full", "session_id": 1}, "wrong_scope"),
    ])
    def test_each_refusal_emits_its_reason_anonymously(
            self, api_client: Any, resolver: MagicMock, recorders: tuple[MagicMock, MagicMock],
            token: str, row: Any, reason: str) -> None:
        ph, _ = recorders
        resolver.return_value = row
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        api_client.post("/api/mcp", content="{}", headers=headers)
        events = [c.kwargs for c in ph.capture.call_args_list
                  if c.kwargs["event"] == "mcp_auth_refused"]
        assert [e["properties"]["reason"] for e in events] == [reason]
        assert events[0]["distinct_id"] == "anonymous"

    def test_a_gate_check_before_auth_is_reported_too(
            self, api_client: Any, recorders: tuple[MagicMock, MagicMock]) -> None:
        ph, _ = recorders
        api_client.get("/api/mcp")
        reasons = [c.kwargs["properties"]["reason"] for c in ph.capture.call_args_list
                   if c.kwargs["event"] == "mcp_auth_refused"]
        assert reasons == ["method"]


class TestTheClient:
    def test_off_whenever_emit_is_off(self) -> None:
        with patch(f"{_O}.posthog") as ph:
            ph.disabled = True
            assert observability._mcp_analytics() is None

    def test_built_once_without_exception_autocapture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(observability, "_mcp_analytics_client", None)
        with patch(f"{_O}.posthog") as ph, patch(f"{_O}.telemetry_muted", return_value=False), \
             patch("posthog.mcp.PostHogMCP") as cls:
            ph.disabled, ph.api_key, ph.host = False, "phc_test", "https://example.test"
            first = observability._mcp_analytics()
            second = observability._mcp_analytics()
        assert first is second is cls.return_value
        cls.assert_called_once()
        assert cls.call_args.kwargs["mcp_exception_autocapture"] is False
        assert cls.call_args.args == ("phc_test",)

    def test_capture_never_raises(self) -> None:
        broken = MagicMock()
        broken.capture_tool_call.side_effect = RuntimeError("down")
        with patch(f"{_O}._mcp_analytics", return_value=broken):
            observability.capture_mcp_analytics("lem_whoami", 1, None, 1.0, "ok", None)

    def test_the_trackers_never_raise(self) -> None:
        with patch(f"{_O}._emit", side_effect=RuntimeError("down")):
            observability.track_mcp_tool_call(1, "t", "ok", None, "mcp", 1, None, 0)
            observability.track_mcp_auth_refused("missing")

    def test_both_event_specs_are_registered(self) -> None:
        tool = observability.EVENTS["mcp_tool_call"]
        assert {f.name for f in tool.fields if f.filtered} == {"tool", "outcome", "client_name",
                                                              "scope"}
        refused = observability.EVENTS["mcp_auth_refused"]
        assert [f.name for f in refused.fields] == ["reason"] and refused.fields[0].filtered


class TestTheTokenIsNeverLogged:
    def test_no_log_record_carries_the_token(
            self, api_client: Any, resolver: MagicMock, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)
        with admitted(resolver), patch("cqc_lem.api.main.get_posts", side_effect=RuntimeError("x")), \
             patch("cqc_lem.api.main.get_generation_status", return_value=None):
            rpc(api_client, "tools/list")
            call(api_client, "lem_get_generation_status")
            call(api_client, "lem_list_posts")
            call(api_client, "lem_list_posts", {"bogus": TOKEN})
            call(api_client, "lem_list_outreach", {"kind": "dms"})
            rpc(api_client, "resources/read", {"uri": "lem://account/overview"})
        api_client.post(f"/api/mcp?token={TOKEN}", content="{}",
                        headers={"Content-Type": "application/json"})
        assert caplog.records, "the run logged nothing, so this test would prove nothing"
        # The TEST's own client logs the URL it was told to send, token and all: that is the
        # caller's side of the deliberately bad `?token=` request, which production never logs
        # (nginx `access_log off` for `/api/mcp?...`, owner ruling O15). Exactly that one line is
        # excluded; the loopback's inner requests log under the same `httpx2` logger and stay checked.
        own_bad_url = f"/api/mcp?token={TOKEN}"
        server_side = [r for r in caplog.records if own_bad_url not in r.getMessage()]
        assert len(caplog.records) - len(server_side) <= 1
        assert any("session_token=cookie" in r.getMessage() for r in server_side), (
            "the inner requests must be among the records under check")
        for record in server_side:
            rendered = record.getMessage() + repr(record.__dict__)
            assert TOKEN not in rendered, record.getMessage()
