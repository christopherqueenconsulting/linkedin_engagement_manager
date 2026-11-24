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

from cqc_lem.api.mcp.tools import tools_for_scope

pytestmark = pytest.mark.unit

_SPEC = importlib.util.spec_from_file_location(
    "mcp_smoke", Path(__file__).resolve().parents[3] / "scripts" / "mcp_smoke.py")
smoke = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(smoke)

_TOKEN = "lem_mcp_" + "smokecanary" * 3


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
        else:
            result = {"structuredContent": {}}
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    return httpx2.Client(transport=httpx2.MockTransport(handle)), seen


def _run(client: httpx2.Client, *extra: str) -> tuple[int, str]:
    out = io.StringIO()
    code = smoke.main(["--read-only", "--base", "https://lem.example", "--token", _TOKEN, *extra],
                      out=out, client=client)
    return code, out.getvalue()


@pytest.mark.parametrize("scope", ["mcp", "mcp_outreach"])
def test_a_healthy_server_passes_and_every_read_tool_is_called(scope: str) -> None:
    client, seen = _server(scope)
    code, text = _run(client)
    assert code == 0, text
    called = [b["params"]["name"] for b in seen if b["method"] == "tools/call"]
    assert set(called) == {s.name for s in tools_for_scope(scope)}
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
