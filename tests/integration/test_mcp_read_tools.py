"""The MCP read tools against a live MySQL and Redis, driven by the REAL `mcp` SDK client (P3).

`docs/agent-mcp-plan.md` §9 Integration. The client is `mcp.client.streamable_http_client` over an
`httpx2.AsyncClient` on `httpx2.ASGITransport(app)`, inside one `app.router.lifespan_context(app)`,
so the whole chain runs for real: the SDK client's own `initialize`, the gate (resolve, flag,
limits, access record), the SDK server, the tool registry, the loopback with its dispatch binding,
`_scope_checked` and the route handlers reading real rows. Nothing that touches identity is
patched. Two users prove isolation; a revoke mid-run proves the next call is refused.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import httpx2
import mysql.connector
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from cqc_lem.utilities import db
from cqc_lem.utilities.crypto import hash_session_token

pytestmark = pytest.mark.integration

_EMAIL_A = "mcp-read-2686-a@example.test"
_EMAIL_B = "mcp-read-2686-b@example.test"
_BASE = "http://testserver"


def _schema_available() -> bool:
    try:
        connection = mysql.connector.connect(connect_timeout=3, **db._get_mysql_config())
    except Exception:  # noqa: BLE001 - unset/incomplete DB env means "no server here", so skip
        return False
    try:
        cursor = connection.cursor()
        cursor.execute("SHOW COLUMNS FROM sessions LIKE 'scope'")
        present = bool(cursor.fetchone())
        cursor.close()
        return present
    except Exception:  # noqa: BLE001
        return False
    finally:
        connection.close()


def _redis_available() -> bool:
    from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client

    client = shared_redis_client()
    if client is None:
        return False
    try:
        client.delete("mcp:kill")
        return True
    except Exception:  # noqa: BLE001
        return False


def _exec(sql: str, params: tuple = (), fetch: bool = False) -> Any:
    connection = db.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall() if fetch else None
        connection.commit()
        return rows
    finally:
        cursor.close()
        connection.close()


def _cleanup() -> None:
    for email in (_EMAIL_A, _EMAIL_B):
        rows = _exec("SELECT id FROM users WHERE email=%s", (email,), fetch=True)
        for row in rows:
            for table in ("auth_audit_log", "sessions", "posts"):
                _exec(f"DELETE FROM {table} WHERE user_id=%s", (row["id"],))
        _exec("DELETE FROM users WHERE email=%s", (email,))


@pytest.fixture
def users(monkeypatch: pytest.MonkeyPatch) -> Any:
    if not _schema_available():
        pytest.skip("no migrated MySQL schema available for the MCP integration test")
    if not _redis_available():
        pytest.skip("no Redis: the gate fails closed without one")
    monkeypatch.setenv("PUBLIC_BASE_URL", _BASE)
    monkeypatch.setenv("MCP_SERVER_ENABLED", "true")
    monkeypatch.delenv("MCP_SERVER_KILL", raising=False)
    _cleanup()
    a, b = db.add_user_by_email(_EMAIL_A), db.add_user_by_email(_EMAIL_B)
    assert a and b
    from cqc_lem.utilities.db import PostType

    assert db.insert_planned_post(a, datetime.now(timezone.utc) + timedelta(days=2), PostType.TEXT,
                                  "awareness")
    post_a = _exec("SELECT id FROM posts WHERE user_id=%s", (a,), fetch=True)[0]["id"]
    # The tools read through the app's pooled connections, in the threadpool. Earlier tests in this
    # worker leave pooled connections behind, and one of them was observed (CI, and locally under
    # `-n 4 --dist loadfile`) to read a snapshot from BEFORE the rows above existed. Start the reads
    # on a fresh pool, so this test proves the MCP chain and not another file's connection state.
    from cqc_lem.platform.db.connection import reset_connection_pool

    reset_connection_pool()
    yield {"a": a, "b": b, "post_a": post_a}
    _cleanup()


def _token(user_id: int, scope: str = "mcp") -> str:
    token = db.create_session(user_id, label="Integration agent", scope=scope, ttl_hours=24,
                              token_prefix=db.MCP_TOKEN_PREFIX)
    assert token
    return token


@asynccontextmanager
async def _session(token: str) -> AsyncIterator[ClientSession]:
    from cqc_lem.api.main import app

    async with app.router.lifespan_context(app):
        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=_BASE,
                                  headers={"Authorization": f"Bearer {token}"}, timeout=30)
        async with http, streamable_http_client(f"{_BASE}/api/mcp", http_client=http) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


# Quarantined (non-strict xfail): intermittently reads `[]` for a just-committed post under
# `-n 4 --dist loadfile`, even after the fixture's reset_connection_pool(). Root cause is open in
# #2732; until then it must not fail unrelated PRs. It still runs, and an XPASS is reported.
@pytest.mark.xfail(reason="intermittent stale/empty read of a committed row, #2732", strict=False)
async def test_initialize_list_and_call_with_the_real_client(users: Any) -> None:
    token = _token(users["a"])
    async with _session(token) as session:
        listed = await session.list_tools()
        names = {tool.name for tool in listed.tools}
        assert {"lem_whoami", "lem_list_posts", "lem_get_post"} <= names
        assert "lem_list_outreach" not in names, "an mcp token never sees the PII lists"

        whoami = await session.call_tool("lem_whoami", {})
        assert whoami.is_error is False
        assert whoami.structured_content["scope"] == "mcp"
        assert "stripe_customer_id" not in str(whoami.structured_content)

        posts = await session.call_tool("lem_list_posts", {"status_filter": "planning"})
        assert posts.is_error is False
        ids = [p["post_id"] for p in posts.structured_content["posts"]]
        assert ids == [users["post_a"]]

        one = await session.call_tool("lem_get_post", {"post_id": users["post_a"]})
        assert one.is_error is False
        assert one.structured_content["post"]["post_id"] == users["post_a"]


async def test_two_users_are_isolated(users: Any) -> None:
    token_b = _token(users["b"], scope="mcp_outreach")
    async with _session(token_b) as session:
        posts = await session.call_tool("lem_list_posts", {"status_filter": "planning"})
        assert posts.structured_content["posts"] == []
        stolen = await session.call_tool("lem_get_post", {"post_id": users["post_a"]})
        assert stolen.is_error is True
        assert stolen.structured_content["error"]["code"] == "not_found"
        outreach = await session.call_tool("lem_list_outreach", {"kind": "dms"})
        assert outreach.is_error is False and outreach.structured_content["items"] == []


async def test_a_revoke_mid_run_refuses_the_next_call(users: Any) -> None:
    token = _token(users["a"])
    session_id = _exec("SELECT id FROM sessions WHERE session_token=%s",
                       (hash_session_token(token),), fetch=True)[0]["id"]
    async with _session(token) as session:
        assert (await session.call_tool("lem_get_generation_status", {})).is_error is False
        assert db.revoke_session(users["a"], session_id)
        with pytest.raises(Exception):  # the gate answers 401 before the SDK sees the call
            await session.call_tool("lem_get_generation_status", {})
