"""`scripts/mcp_smoke.py`: its checks pass on a healthy server and FAIL on a wrong one.

A fake MCP server (an `httpx2.MockTransport`) answers JSON-RPC, so the script's own logic is under
test: which calls it makes, what it compares, and that a mismatch is a non-zero exit. The live run
against a real stack is the script's job, not this file's.
"""

import importlib.util
import io
import json
from pathlib import Path
from typing import Any, Optional

import httpx2
import pytest

from cqc_lem.api.mcp.tools import TOOLS_BY_NAME, tools_for_scope

pytestmark = pytest.mark.unit

_SPEC = importlib.util.spec_from_file_location(
    "mcp_smoke", Path(__file__).resolve().parents[3] / "scripts" / "mcp_smoke.py")
smoke = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(smoke)

_TOKEN = "lem_mcp_" + "smokecanary" * 3


@pytest.fixture(autouse=True)
def _restore_http_loggers() -> Any:
    """`smoke.main()` quiets the `httpx`/`httpx2` loggers for the whole process, as a CLI should.

    In a test process that leaks into every later test on the same worker: the MCP caplog test
    (`test_mcp_analytics.py`) asserts the loopback's inner requests ARE logged, and silently loses
    them when it runs after this file. Restore whatever level each logger had.
    """
    import logging

    saved = {name: logging.getLogger(name).level for name in ("httpx", "httpx2")}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


def _server(scope: str = "mcp", tools: Optional[list[str]] = None, failing: str = "",
            query_status: int = 400) -> tuple[httpx2.Client, list[dict]]:
    seen: list[dict] = []
    names = tools if tools is not None else [s.name for s in tools_for_scope(scope)]

    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.params.get("token"):
            return httpx2.Response(query_status, json={"reason": "token_in_url_refused"})
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {_TOKEN}":
            return httpx2.Response(401, json={"reason": "invalid_token"})
        body = json.loads(request.content)
        seen.append(body)
        method, params = body["method"], body.get("params") or {}
        if method == "initialize":
            result: Any = {"serverInfo": {"name": "lem"}}
        elif method == "tools/list":
            result = {"tools": [{"name": n} for n in names]}
        elif params.get("name") == failing:
            result = {"isError": True, "structuredContent": {"error": {"code": "boom"}}}
        elif params.get("name") == "lem_whoami":
            result = {"structuredContent": {"scope": scope, "account_name": "jane"}}
        elif params.get("name") == "lem_list_posts":
            result = {"structuredContent": {"posts": [{"post_id": 4}]}}
        elif params.get("name") in _WRITE_ANSWERS:
            result = _WRITE_ANSWERS[params["name"]](params.get("arguments") or {}, names, seen)
        else:
            result = {"structuredContent": {}}
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    return httpx2.Client(transport=httpx2.MockTransport(handle)), seen


def _error(code: str) -> dict:
    return {"isError": True, "structuredContent": {"error": {"code": code}}}


def _queue_dm(args: dict, _names: list, seen: list) -> dict:
    first = next((b["params"]["arguments"] for b in seen
                  if b.get("params", {}).get("name") == "lem_queue_dm"), None)
    if first is not None and first is not args and first.get("client_request_id") == args.get(
            "client_request_id"):
        if first != args:
            return _error("client_request_id_reused")
        return {"structuredContent": {"id": 5, "status": "pending", "source": "mcp",
                                      "replayed": True}}
    return {"structuredContent": {"id": 5, "status": "pending", "source": "mcp"}}


def _approve(_args: dict, names: list, _seen: list) -> dict:
    if "lem_approve_item" in names:
        return {"structuredContent": {"status": "approved"}}
    return _error("agent_may_not_approve")


_WRITE_ANSWERS = {
    "lem_queue_dm": _queue_dm,
    "lem_edit_lead_signal_draft": lambda a, n, s: _error("invalid_arguments"),
    "lem_approve_item": _approve,
}


def _run(client: httpx2.Client, *extra: str, mode: str = "--read-only") -> tuple[int, str]:
    out = io.StringIO()
    code = smoke.main([mode, "--base", "https://lem.example", "--token", _TOKEN, *extra],
                      out=out, client=client)
    return code, out.getvalue()


@pytest.mark.parametrize("scope", ["mcp", "mcp_outreach"])
def test_a_healthy_server_passes_and_every_read_tool_is_called(scope: str) -> None:
    client, seen = _server(scope)
    code, text = _run(client)
    assert code == 0, text
    called = [b["params"]["name"] for b in seen if b["method"] == "tools/call"]
    assert set(called) == {s.name for s in tools_for_scope(scope) if not s.write}
    assert {"name": "lem_get_post", "arguments": {"post_id": 4}} in [
        b["params"] for b in seen if b["method"] == "tools/call"]
    if scope == "mcp_outreach":
        assert called.count("lem_list_outreach") == 6
    assert _TOKEN not in text


def test_a_tools_list_that_drifts_from_the_registry_fails() -> None:
    client, _ = _server(tools=["lem_whoami", "lem_extra"])
    code, text = _run(client)
    assert code == 1 and "[FAIL] tools/list equals the registry" in text


def test_a_tool_error_fails() -> None:
    client, _ = _server(failing="lem_get_dashboard")
    code, text = _run(client)
    assert code == 1 and "[FAIL] tool lem_get_dashboard - boom" in text


def test_a_query_token_that_is_not_refused_fails() -> None:
    client, _ = _server(query_status=200)
    code, text = _run(client)
    assert code == 1 and "[FAIL] a query-string token is refused" in text


def test_mint_local_is_refused_for_a_remote_base() -> None:
    out = io.StringIO()
    assert smoke.main(["--base", "https://lem.example", "--mint-local-for", "a@b.c"], out=out) == 2
    assert "not localhost" in out.getvalue()


def test_no_token_is_bad_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEM_MCP_TOKEN", raising=False)
    out = io.StringIO()
    assert smoke.main(["--base", "https://lem.example"], out=out) == 2


def test_a_standard_account_may_omit_the_approval_tool() -> None:
    client, _ = _server("mcp_outreach", tools=[s.name for s in tools_for_scope("mcp_outreach")
                                               if not s.approval])
    code, text = _run(client)
    assert code == 0, text


_RECIPIENT = "https://www.linkedin.com/in/smoke-test/"


def _approvals(seen: list) -> list:
    return [b for b in seen if b.get("params", {}).get("name") == "lem_approve_item"]


@pytest.mark.parametrize("kind,listed", [("sandbox", True), ("standard", False)])
def test_the_write_checks_pass_on_a_healthy_server(kind: str, listed: bool) -> None:
    names = [s.name for s in tools_for_scope("mcp_outreach") if listed or not s.approval]
    client, seen = _server("mcp_outreach", tools=names)
    code, text = _run(client, "--recipient", _RECIPIENT, "--account-kind", kind, mode="--write")
    assert code == 0, text
    assert "[PASS] lem_queue_dm lands pending with source=mcp" in text
    assert "[PASS] a retried write replays instead of writing twice" in text
    assert "[PASS] a reused request id is refused" in text
    label = ("lem_approve_item approves (sandbox)" if kind == "sandbox"
             else "approve is refused on a standard account")
    assert f"[PASS] {label}" in text
    queued = [b for b in seen if b.get("params", {}).get("name") == "lem_queue_dm"]
    assert queued[0]["params"]["arguments"]["recipient_profile_url"] == _RECIPIENT
    assert TOOLS_BY_NAME["lem_queue_dm"].write
    # Without a local DB to cancel through, any kind but sandbox tells the operator to cancel it.
    assert ("[ACTION] cancel the smoke DM #5" in text) is (kind != "sandbox")


def test_an_internal_account_never_approves_even_when_the_tool_is_listed() -> None:
    client, seen = _server("mcp_outreach")  # lem_approve_item listed
    code, text = _run(client, "--recipient", _RECIPIENT, "--account-kind", "internal",
                      mode="--write")
    assert code == 0, text
    assert _approvals(seen) == []
    assert "[SKIP] lem_approve_item" in text and "REAL send" in text
    assert "[ACTION] cancel the smoke DM #5" in text


def test_a_listed_approval_tool_on_a_declared_standard_account_fails() -> None:
    client, seen = _server("mcp_outreach")  # listed, but the operator says standard
    code, text = _run(client, "--recipient", _RECIPIENT, "--account-kind", "standard",
                      mode="--write")
    assert code == 1 and "[FAIL] lem_approve_item is not listed on a standard account" in text


def test_an_approval_that_lands_on_a_standard_account_fails() -> None:
    names = [s.name for s in tools_for_scope("mcp_outreach") if not s.approval]
    client, _ = _server("mcp_outreach", tools=names)
    _WRITE_ANSWERS["lem_approve_item"], saved = (
        lambda a, n, s: {"structuredContent": {"status": "approved"}}), _WRITE_ANSWERS[
        "lem_approve_item"]
    try:
        code, text = _run(client, "--recipient", _RECIPIENT, "--account-kind", "standard",
                          mode="--write")
    finally:
        _WRITE_ANSWERS["lem_approve_item"] = saved
    assert code == 1 and "[FAIL] approve is refused on a standard account" in text


@pytest.mark.parametrize("canceled", [True, False])
def test_the_smoke_dm_is_canceled_through_the_local_db(canceled: bool) -> None:
    names = [s.name for s in tools_for_scope("mcp_outreach") if not s.approval]
    client, _ = _server("mcp_outreach", tools=names)
    out = io.StringIO()
    run = smoke.Smoke("https://lem.example", out, client)
    calls: list[int] = []

    def cancel(dm_id: int) -> bool:
        calls.append(dm_id)
        return canceled

    smoke.run_write_checks(run, _TOKEN, _RECIPIENT, "standard", cancel)
    assert calls == [5]
    assert ("[PASS] the smoke DM was canceled" in out.getvalue()) is canceled
    assert "[ACTION]" not in out.getvalue()


def test_the_write_checks_skip_without_an_outreach_token() -> None:
    client, _ = _server("mcp")
    code, text = _run(client, "--recipient", _RECIPIENT, "--account-kind", "sandbox",
                      mode="--write")
    assert code == 0 and "[SKIP] write checks" in text


@pytest.mark.parametrize("extra", [(), ("--recipient", _RECIPIENT), ("--account-kind", "sandbox")])
def test_write_needs_a_recipient_and_an_account_kind(extra: tuple) -> None:
    client, _ = _server("mcp_outreach")
    code, text = _run(client, *extra, mode="--write")
    assert code == 2 and "--write needs --recipient and --account-kind" in text


# --- the optional settings round trip (#2800) ---------------------------------------------------------


def _settings_server(scope: str = "mcp", listed: bool = True, restores: bool = True) -> tuple:
    """A fake server whose tone really changes and (when `restores`) really comes back on undo."""
    state = {"tone": "warm"}
    names = [s.name for s in tools_for_scope(scope) if listed or not s.flag]
    seen: list[dict] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.params.get("token"):
            return httpx2.Response(400, json={"reason": "token_in_url_refused"})
        if request.headers.get("authorization", "") != f"Bearer {_TOKEN}":
            return httpx2.Response(401, json={"reason": "invalid_token"})
        body = json.loads(request.content)
        seen.append(body)
        params = body.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        result: Any = {"structuredContent": {}}
        if body["method"] == "initialize":
            result = {"serverInfo": {"name": "lem"}}
        elif body["method"] == "tools/list":
            result = {"tools": [{"name": n} for n in names]}
        elif name == "lem_whoami":
            result = {"structuredContent": {"scope": scope, "account_name": "jane"}}
        elif name == "lem_list_posts":
            result = {"structuredContent": {"posts": [{"post_id": 4}]}}
        elif name == "lem_get_engagement_settings":
            result = {"structuredContent": {"preferences": {"tone": state["tone"]}}}
        elif name == "lem_update_settings":
            if "max_comments_per_day" in args.get("changes", {}):
                result = _error("cap_not_editable")
            else:
                before, state["tone"] = state["tone"], args["changes"]["tone"]
                result = {"structuredContent": {"changed": {"tone": {
                    "before": before, "after": state["tone"]}}, "undo_id": "abcdef0123456789"}}
        elif name == "lem_undo_settings_change":
            if restores:
                state["tone"] = "warm"
            result = {"structuredContent": {"restored": {"tone": {}}}}
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    return httpx2.Client(transport=httpx2.MockTransport(handle)), seen


def _settings_calls(seen: list) -> list:
    return [b for b in seen if b.get("params", {}).get("name") == "lem_update_settings"]


def test_the_settings_round_trip_passes_on_a_healthy_server() -> None:
    client, _ = _settings_server()
    code, text = _run(client, "--settings")
    assert code == 0, text
    assert "[PASS] lem_update_settings changes tone and reports before/after" in text
    assert "[PASS] a daily cap is refused" in text
    assert "[PASS] tone is back to its old value" in text


def test_a_settings_undo_that_does_not_restore_fails() -> None:
    client, _ = _settings_server(restores=False)
    code, text = _run(client, "--settings")
    assert code == 1 and "[FAIL] tone is back to its old value" in text


def test_the_settings_round_trip_is_skipped_when_the_tools_are_not_listed() -> None:
    client, seen = _settings_server(listed=False)
    code, text = _run(client, "--settings")
    assert code == 0, text
    assert "[SKIP] settings checks" in text
    assert _settings_calls(seen) == []


def test_the_settings_round_trip_never_runs_without_the_flag_argument() -> None:
    client, seen = _settings_server()
    code, _text = _run(client)
    assert code == 0
    assert _settings_calls(seen) == []


def test_a_tools_list_without_the_flagged_tools_matches_the_registry() -> None:
    client, _ = _server("mcp", tools=[s.name for s in tools_for_scope("mcp") if not s.flag])
    code, text = _run(client)
    assert code == 0, text
