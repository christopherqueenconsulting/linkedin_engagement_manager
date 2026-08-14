"""The `agent` scope against a live MySQL and a real HTTP request (issue #1026).

The unit tests call the guards directly, with the scope ContextVar set by hand. That proves the
guards refuse what they are handed — it does NOT prove the thing the whole design rests on: that by
the time a handler calls a guard, the resolver has actually STAMPED the scope for this request.
Everything in between is real machinery — the ASGI middleware that resets the ContextVar per
request, `resolve_session` reading the row back out of MySQL, `_scope_checked` deciding the path is
on the surface and stamping the scope. A static call-site count cannot see any of it, and a mocked
`get_session_user_id` deletes it entirely.

So these drive a REAL request, on a REAL agent session row, through the REAL resolver. Nothing that
touches identity is patched.
"""

from datetime import datetime, timedelta, timezone

import mysql.connector
import pytest

from cqc_lem.utilities import db

pytestmark = pytest.mark.integration

_EMAIL = "agent-token-1026@example.test"


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


def _cleanup():
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))
    _exec("DELETE FROM auth_audit_log WHERE email=%s", (_EMAIL,))


@pytest.fixture
def user_id():
    if not _schema_available():
        pytest.skip("no migrated MySQL schema available for the #1026 integration test")
    _cleanup()
    uid = db.add_user_by_email(_EMAIL)
    assert uid, "test user was not created"
    yield uid
    _cleanup()


@pytest.fixture
def agent_token(user_id):
    """A real agent session, minted the way the endpoint mints it."""
    token = db.create_session(user_id, label="Headless agent",
                              scope=db.SESSION_SCOPE_AGENT, ttl_hours=90 * 24)
    assert token
    return token


@pytest.fixture
def human_token(user_id):
    return db.create_session(user_id, label="Browser")


@pytest.fixture
def pending_request(user_id):
    request_id = db.insert_connection_request(
        user_id, "https://www.linkedin.com/in/someone-1026/", message="Hello",
        status=db.ConnectionRequestStatus.PENDING)
    assert request_id
    return request_id


class TestTheGuardIsReachableAtRuntime:
    """The single claim static analysis cannot make: the ContextVar is populated by the time the
    handler asks.
    """

    def test_an_agent_session_is_refused_when_it_tries_to_approve(self, api_client, agent_token,
                                                                  pending_request):
        r = api_client.put("/api/connection_request",
                       json={"session_token": agent_token, "request_id": pending_request,
                             "action": "approve"})
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "agent_may_not_approve"

    def test_the_refusal_leaves_the_row_unapproved(self, api_client, agent_token, pending_request):
        """A 403 that still wrote the row would be theatre."""
        api_client.put("/api/connection_request",
                   json={"session_token": agent_token, "request_id": pending_request,
                         "action": "approve"})
        row = db.get_connection_request(pending_request)
        assert str(row["status"]) == str(db.ConnectionRequestStatus.PENDING)

    def test_the_same_call_on_a_human_session_approves(self, api_client, human_token, pending_request):
        """The guard must refuse the agent WITHOUT breaking the endpoint for its real user —
        otherwise a green refusal test is indistinguishable from a broken route.
        """
        r = api_client.put("/api/connection_request",
                       json={"session_token": human_token, "request_id": pending_request,
                             "action": "approve"})
        assert r.status_code == 200, r.text
        row = db.get_connection_request(pending_request)
        assert str(row["status"]) == str(db.ConnectionRequestStatus.APPROVED)

    def test_an_agent_may_still_save_a_draft(self, api_client, agent_token, pending_request):
        """Queueing is the point — the narrowing must not swallow the thing the token exists for."""
        r = api_client.put("/api/connection_request",
                       json={"session_token": agent_token, "request_id": pending_request,
                             "message": "A rewritten opener."})
        assert r.status_code == 200, r.text
        assert db.get_connection_request(pending_request)["message"] == "A rewritten opener."


class TestApprovalAtCreateTime:
    """The half with no `action` field for the other guard to see."""

    def test_an_agent_cannot_create_an_already_approved_dm(self, api_client, agent_token):
        when = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        r = api_client.post("/api/schedule_dm",
                        json={"session_token": agent_token,
                              "recipient_profile_url": "https://www.linkedin.com/in/someone-1026/",
                              "message": "Hi there", "scheduled_datetime": when,
                              "status": "approved"})
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "agent_may_not_approve"

    def test_an_agent_may_create_a_pending_dm(self, api_client, agent_token):
        when = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        r = api_client.post("/api/schedule_dm",
                        json={"session_token": agent_token,
                              "recipient_profile_url": "https://www.linkedin.com/in/someone-1026/",
                              "message": "Hi there", "scheduled_datetime": when})
        assert r.status_code == 200, r.text

    def test_the_account_auto_approve_default_does_not_approve_for_an_agent(
            self, api_client, agent_token, user_id):
        """`connection_request_mode` is `auto_approve` out of the box, so naming no status at all
        used to land an APPROVED row on nearly every account.
        """
        db.update_engagement_preferences(user_id, {"connection_request_mode": "auto_approve"})
        r = api_client.post("/api/connection_request",
                        json={"session_token": agent_token,
                              "recipient_profile_url": "https://www.linkedin.com/in/someone-else/"})
        assert r.status_code == 200, r.text
        row = db.get_connection_request(r.json()["detail"]["request_id"])
        assert str(row["status"]) == str(db.ConnectionRequestStatus.PENDING)


class TestTheRestOfTheNarrowing:
    def test_an_agent_cannot_reconfigure_the_account(self, api_client, agent_token):
        """This write sets the approval modes and the per-day caps — it re-opens everything else."""
        r = api_client.put("/api/user/engagement-preferences",
                       json={"session_token": agent_token, "connection_request_mode": "auto_approve",
                             "max_dms_per_day": 500})
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "agent_may_not_configure"

    def test_an_agent_may_still_read_its_preferences(self, api_client, agent_token):
        r = api_client.get(f"/api/user/engagement-preferences?session_token={agent_token}")
        assert r.status_code == 200, r.text

    @pytest.mark.parametrize("method,path", [
        ("get", "/api/user/security"),
        # The minting endpoints are POST-only. Asking with the WRONG verb proves nothing here: an
        # unmatched GET falls through to the SPA catch-all and returns index.html with a 200, which
        # looks like a pass and is really the frontend being served.
        ("post", "/api/user/agent-token"),
        ("post", "/api/user/extension-token"),
    ])
    def test_a_path_off_the_surface_is_refused_before_any_handler(self, api_client, agent_token,
                                                                  method, path):
        """A stolen agent token must not be able to mint its successor."""
        kwargs = ({"params": {"session_token": agent_token}} if method == "get"
                  else {"json": {"session_token": agent_token}})
        r = getattr(api_client, method)(path, **kwargs)
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "session_scope_forbidden"


class TestTheGrantedTtlSurvivesBeingUsed:
    def test_a_real_request_does_not_slide_the_agent_expiry_down_to_the_idle_window(
            self, api_client, agent_token):
        """resolve_session slides EVERY other session to now + SESSION_IDLE_HOURS. Doing that here
        rewrote the 90-day token to 24 hours on its FIRST request — the weekly agent dead every run,
        which is the exact failure `ttl_hours` was added to prevent.
        """
        before = _exec("SELECT expires_at FROM sessions WHERE session_token=%s",
                       (db.hash_session_token(agent_token),), fetch=True)[0]["expires_at"]

        r = api_client.get(f"/api/user/engagement-preferences?session_token={agent_token}")
        assert r.status_code == 200, r.text

        after = _exec("SELECT expires_at, last_seen_at FROM sessions WHERE session_token=%s",
                      (db.hash_session_token(agent_token),), fetch=True)[0]
        assert after["expires_at"] == before, "the granted TTL was rewritten by using the token"
        assert after["last_seen_at"] is not None, "the session should still record its use"
        # And it is genuinely long — not the idle window wearing a 90-day label.
        remaining = after["expires_at"].replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
        assert remaining > timedelta(days=60)

    def test_a_browser_session_still_slides(self, api_client, human_token):
        """The fix must not turn every other session into a fixed-expiry one."""
        _exec("UPDATE sessions SET expires_at=%s WHERE session_token=%s",
              (datetime.now(timezone.utc) + timedelta(hours=1),
               db.hash_session_token(human_token)))
        api_client.get(f"/api/user/engagement-preferences?session_token={human_token}")
        after = _exec("SELECT expires_at FROM sessions WHERE session_token=%s",
                      (db.hash_session_token(human_token),), fetch=True)[0]["expires_at"]
        remaining = after.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
        assert remaining > timedelta(hours=2), "an active browser session must keep sliding forward"
