"""MCP tokens against a live MySQL (and Redis, where present): mint, resolve, use, revoke (P2).

`docs/agent-mcp-plan.md` §9 integration rows for P2. The unit lane proves each piece with the
resolver stubbed; this drives real session rows through the real resolver, the real gate and the
real kernel, so the fixed TTL, the `last_seen_at` throttle, the dispatch binding and revocation are
checked end to end. Nothing that touches identity is patched.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import mysql.connector
import pytest

from cqc_lem.utilities import db
from cqc_lem.utilities.crypto import hash_session_token

pytestmark = pytest.mark.integration

_EMAIL = "mcp-token-2685@example.test"
_ACCEPT = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
_INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "integration", "version": "0"}}}


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


def _exec(sql: str, params=(), fetch: bool = False):
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


def _row(token: str) -> dict:
    return _exec("SELECT id, scope, expires_at, last_seen_at, revoked_at FROM sessions "
                 "WHERE session_token=%s", (hash_session_token(token),), fetch=True)[0]


def _cleanup(user_id=None):
    if user_id:
        _exec("DELETE FROM auth_audit_log WHERE user_id=%s", (user_id,))
        _exec("DELETE FROM sessions WHERE user_id=%s", (user_id,))
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))


@pytest.fixture
def user_id(monkeypatch):
    if not _schema_available():
        pytest.skip("no migrated MySQL schema available for the MCP integration test")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://testserver")
    monkeypatch.setenv("MCP_SERVER_ENABLED", "true")
    monkeypatch.delenv("MCP_SERVER_KILL", raising=False)
    _cleanup()
    uid = db.add_user_by_email(_EMAIL)
    assert uid, "test user was not created"
    yield uid
    _cleanup(uid)


@pytest.fixture
def mcp_token(user_id):
    token = db.create_session(user_id, label="Integration agent", scope=db.SESSION_SCOPE_MCP,
                              ttl_hours=30 * 24, token_prefix=db.MCP_TOKEN_PREFIX)
    assert token and token.startswith("lem_mcp_")
    return token


def _events(user_id: int, event: str) -> list:
    return _exec("SELECT details FROM auth_audit_log WHERE user_id=%s AND event=%s",
                 (user_id, event), fetch=True)


class TestResolve:
    def test_the_scope_and_the_fixed_ttl(self, mcp_token):
        found = db.resolve_session_checked(mcp_token)
        assert found["scope"] == "mcp"
        expires = found["expires_at"].replace(tzinfo=timezone.utc)
        # A day of slack either side: the claim is "about 30 days, fixed", not the server clock.
        assert timedelta(days=29) < expires - datetime.now(timezone.utc) < timedelta(days=31)
        before = _row(mcp_token)["expires_at"]
        db.resolve_session_checked(mcp_token)
        assert _row(mcp_token)["expires_at"] == before, "a fixed-TTL seat never slides"

    def test_last_seen_is_written_at_most_once_a_minute(self, mcp_token):
        recent = (datetime.now(timezone.utc) - timedelta(seconds=10)).replace(tzinfo=None,
                                                                               microsecond=0)
        _exec("UPDATE sessions SET last_seen_at=%s WHERE session_token=%s",
              (recent, hash_session_token(mcp_token)))
        db.resolve_session(mcp_token)
        assert _row(mcp_token)["last_seen_at"] == recent
        stale = recent - timedelta(minutes=5)
        _exec("UPDATE sessions SET last_seen_at=%s WHERE session_token=%s",
              (stale, hash_session_token(mcp_token)))
        db.resolve_session(mcp_token)
        assert _row(mcp_token)["last_seen_at"] > stale

    def test_a_revoked_token_resolves_to_nothing(self, user_id, mcp_token):
        assert db.revoke_session(user_id, _row(mcp_token)["id"])
        assert db.resolve_session_checked(mcp_token) is None


class TestRawApi:
    def test_an_mcp_token_on_raw_api_is_refused_and_writes_no_audit_row(self, user_id, mcp_token,
                                                                        api_client):
        resp = api_client.get("/api/user/automation-status",
                              params={"session_token": mcp_token})
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["code"] == "session_scope_forbidden"
        assert _exec("SELECT COUNT(*) AS n FROM auth_audit_log WHERE user_id=%s", (user_id,),
                     fetch=True)[0]["n"] == 0


class TestTheGate:
    @pytest.fixture(autouse=True)
    def _redis(self):
        if not _redis_available():
            pytest.skip("no Redis: the gate fails closed without one")

    def test_mint_use_revoke(self, user_id, mcp_token, api_client):
        auth = {**_ACCEPT, "Authorization": f"Bearer {mcp_token}"}
        resp = api_client.post("/api/mcp", json=_INIT, headers=auth)
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"]["serverInfo"]["name"] == "lem"
        assert len(_events(user_id, "mcp_access")) <= 1

        assert db.revoke_session(user_id, _row(mcp_token)["id"])
        resp = api_client.post("/api/mcp", json=_INIT, headers=auth)
        assert resp.status_code == 401 and resp.json()["reason"] == "invalid_token"

    def test_a_query_string_token_is_revoked_and_audited(self, user_id, mcp_token, api_client):
        # The owner notice is an email, not identity: stubbed so CI never sends real mail.
        with patch("cqc_lem.api.mcp.notify.notify_token_event") as notify:
            resp = api_client.post(f"/api/mcp?token={mcp_token}", json=_INIT, headers=_ACCEPT)
        notify.assert_called_once()
        assert resp.status_code == 400 and resp.json()["reason"] == "token_in_url_refused"
        assert _row(mcp_token)["revoked_at"] is not None
        assert len(_events(user_id, "mcp_token_exposed")) == 1

    def test_the_kill_key_is_503(self, mcp_token, api_client):
        from cqc_lem.api.mcp.limits import set_kill_key

        assert set_kill_key(True)
        try:
            resp = api_client.post("/api/mcp", json=_INIT,
                                   headers={**_ACCEPT, "Authorization": f"Bearer {mcp_token}"})
        finally:
            set_kill_key(False)
        assert resp.status_code == 503 and resp.json()["reason"] == "mcp_disabled_by_operator"


class TestRevokeAll:
    def test_every_live_mcp_session_is_revoked_with_an_audit_row(self, user_id, mcp_token):
        other = db.create_session(user_id, scope=db.SESSION_SCOPE_MCP_OUTREACH, ttl_hours=24,
                                  token_prefix=db.MCP_TOKEN_PREFIX)
        browser = db.create_session(user_id, label="Browser")
        revoked = db.revoke_sessions_by_scope(db.MCP_SESSION_SCOPES, reason="integration")
        assert revoked >= 2
        assert _row(mcp_token)["revoked_at"] is not None
        assert _row(other)["revoked_at"] is not None
        assert _row(browser)["revoked_at"] is None, "a browser session is not an MCP seat"
        assert len(_events(user_id, "mcp_sessions_revoked")) == 1
