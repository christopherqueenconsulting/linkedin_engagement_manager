"""The MCP write tools against a live MySQL and Redis, driven by the REAL `mcp` SDK client (W2).

`docs/agent-mcp-plan.md` §9 Integration, W2 row: `lem_queue_dm` writes `status='pending',
source='mcp'` with its `mcp_write` audit row. Plus owner ruling O1 end to end: a standard account
never lists or runs `lem_approve_item`, and a sandbox account approves through it and is audited
`mcp_approval`. Nothing that touches identity, scope or the approval guards is patched.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from cqc_lem.utilities import db

from .test_mcp_read_tools import _exec, _redis_available, _schema_available

pytestmark = pytest.mark.integration

_EMAIL_STANDARD = "mcp-write-2688-a@example.test"
_EMAIL_SANDBOX = "mcp-write-2688-b@example.test"
_BASE = "http://testserver"
_URL = "https://www.linkedin.com/in/w2-integration-recipient/"


def _cleanup() -> None:
    for email in (_EMAIL_STANDARD, _EMAIL_SANDBOX):
        for row in _exec("SELECT id FROM users WHERE email=%s", (email,), fetch=True):
            for table in ("auth_audit_log", "sessions", "scheduled_dms", "connection_requests",
                          "outreach_funnel_targets"):
                _exec(f"DELETE FROM {table} WHERE user_id=%s", (row["id"],))
        _exec("DELETE FROM users WHERE email=%s", (email,))


def _flush_write_keys() -> None:
    from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client

    client = shared_redis_client()
    for pattern in ("mcp:dedupe:*", "mcp:budget:writes_*"):
        for key in client.scan_iter(pattern):
            client.delete(key)


@pytest.fixture
def users(monkeypatch: pytest.MonkeyPatch) -> Any:
    if not _schema_available():
        pytest.skip("no migrated MySQL schema available for the MCP integration test")
    if not _redis_available():
        pytest.skip("no Redis: the gate fails closed without one")
    monkeypatch.setenv("PUBLIC_BASE_URL", _BASE)
    monkeypatch.setenv("MCP_SERVER_ENABLED", "true")
    monkeypatch.setenv("MCP_SERVER_WRITES_ENABLED", "true")
    monkeypatch.delenv("MCP_SERVER_KILL", raising=False)
    _cleanup()
    _flush_write_keys()
    standard, sandbox = db.add_user_by_email(_EMAIL_STANDARD), db.add_user_by_email(_EMAIL_SANDBOX)
    assert standard and sandbox
    _exec("UPDATE users SET account_kind='sandbox' WHERE id=%s", (sandbox,))
    yield {"standard": standard, "sandbox": sandbox}
    _cleanup()
    _flush_write_keys()


def _token(user_id: int) -> str:
    token = db.create_session(user_id, label="Integration agent", scope="mcp_outreach",
                              ttl_hours=24, token_prefix=db.MCP_TOKEN_PREFIX)
    assert token
    return token


async def _session(token: str) -> AsyncIterator[ClientSession]:
    from cqc_lem.api.main import app

    async with app.router.lifespan_context(app):
        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=_BASE,
                                  headers={"Authorization": f"Bearer {token}"}, timeout=30)
        async with http, streamable_http_client(f"{_BASE}/api/mcp", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session


def _when() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()


def _audit(user_id: int, event: str) -> list[dict]:
    return _exec("SELECT event, details FROM auth_audit_log WHERE user_id=%s AND event=%s",
                 (user_id, event), fetch=True)


async def test_lem_queue_dm_writes_a_pending_mcp_row_with_its_audit_row(users: Any) -> None:
    user_id = users["standard"]
    async for session in _session(_token(user_id)):
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert "lem_queue_dm" in names
        assert "lem_approve_item" not in names, "a standard account never lists the approval"

        args = {"recipient_profile_url": _URL, "message": "Hello from the W2 integration test",
                "scheduled_datetime": _when(), "client_request_id": "w2-int-1"}
        result = await session.call_tool("lem_queue_dm", args)
        assert result.is_error is False, result
        dm_id = result.structured_content["id"]
        assert result.structured_content["status"] == "pending"

        again = await session.call_tool("lem_queue_dm", args)
        assert again.structured_content.get("replayed") is True

        refused = await session.call_tool("lem_approve_item", {"kind": "dm", "id": dm_id})
        assert refused.is_error is True
        assert refused.structured_content["error"]["code"] == "agent_may_not_approve"

    rows = _exec("SELECT id, status, source, recipient_profile_url FROM scheduled_dms "
                 "WHERE user_id=%s", (user_id,), fetch=True)
    assert [(r["id"], r["status"], r["source"]) for r in rows] == [(dm_id, "pending", "mcp")]
    writes = _audit(user_id, "mcp_write")
    assert len(writes) == 1
    assert '"lem_queue_dm"' in str(writes[0]["details"]) and str(dm_id) in str(writes[0]["details"])
    assert "Hello from" not in str(writes[0]["details"])
    assert _audit(user_id, "mcp_approval") == []


async def test_a_sandbox_account_approves_through_lem_approve_item(users: Any) -> None:
    user_id = users["sandbox"]
    async for session in _session(_token(user_id)):
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert "lem_approve_item" in names

        queued = await session.call_tool("lem_queue_connection_request",
                                         {"recipient_profile_url": _URL, "message": "Hi"})
        assert queued.is_error is False, queued
        request_id = queued.structured_content["id"]

        approved = await session.call_tool("lem_approve_item",
                                           {"kind": "connection_request", "id": request_id})
        assert approved.is_error is False, approved
        assert approved.structured_content["status"] == "approved"

        # The approval is a pending-only write: approving the same row again changes nothing.
        again = await session.call_tool("lem_approve_item",
                                        {"kind": "connection_request", "id": request_id})
        assert again.is_error is True
        assert again.structured_content["error"]["code"] == "edit_requires_pending"

        target = await session.call_tool("lem_add_outreach_target",
                                         {"target_profile_url": _URL, "draft_text": "Hi"})
        assert target.is_error is False, target
        target_id = target.structured_content["id"]

    session_id = _exec("SELECT id FROM sessions WHERE user_id=%s AND scope='mcp_outreach'",
                       (user_id,), fetch=True)[0]["id"]
    row = _exec("SELECT status, source, approved_by, approved_at FROM connection_requests "
                "WHERE id=%s", (request_id,), fetch=True)[0]
    assert (row["status"], row["source"]) == ("approved", "mcp")
    assert row["approved_by"] == f"mcp:{session_id}" and row["approved_at"] is not None
    created_via = _exec("SELECT created_via FROM outreach_funnel_targets WHERE id=%s",
                        (target_id,), fetch=True)[0]["created_via"]
    assert created_via == "mcp"
    approvals = _audit(user_id, "mcp_approval")
    # The refused second approval wrote nothing (the row was no longer pending), so it left no
    # approval row either.
    assert len(approvals) == 1 and "sandbox" in str(approvals[0]["details"])
    assert len(_audit(user_id, "mcp_write")) == 3
