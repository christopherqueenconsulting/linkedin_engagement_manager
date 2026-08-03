"""Issue #950: the `/api` credential gate against a live, migrated MySQL.

The unit tests patch `_API_ACCESS_TOKEN_SET` and `get_session_user_id`, so they prove the middleware
and the handler in isolation and nothing about the wiring between them. This proves the flow the PR
actually creates end to end: a REAL session row, its token in the `lem_session` cookie, and a gated
`/api` route reached with **no `Authorization` header at all** — the shape every browser request has
now that the SPA ships no bearer.

The negative half matters as much: the gate got looser, so a forged cookie clears the middleware.
It must then die in the handler on a real session lookup that finds nothing, not on a mock that was
told to return None.
"""

import mysql.connector
import pytest

from cqc_lem.utilities import db
from cqc_lem.utilities.env_constants import SESSION_COOKIE_NAME

pytestmark = pytest.mark.integration

_EMAIL = "api-gate-950@example.test"
_TOKEN = "gate-950-non-browser-token"


def _schema_available() -> bool:
    try:
        config = db._get_mysql_config()
        connection = mysql.connector.connect(connect_timeout=3, **config)
    except Exception:  # noqa: BLE001 - unset/incomplete DB env means "no server here", so skip
        return False
    try:
        cursor = connection.cursor()
        cursor.execute("SHOW COLUMNS FROM sessions LIKE 'revoked_at'")
        present = bool(cursor.fetchone())
        cursor.close()
        return present
    except Exception:  # noqa: BLE001
        return False
    finally:
        connection.close()


def _exec(sql: str, params=()) -> None:
    connection = db.get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        connection.commit()
    finally:
        cursor.close()
        connection.close()


@pytest.fixture
def user_id():
    if not _schema_available():
        pytest.skip("no migrated MySQL schema available for the #950 gate integration test")
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))
    _exec("DELETE FROM auth_audit_log WHERE email=%s", (_EMAIL,))
    uid = db.add_user_by_email(_EMAIL)
    assert uid, "test user was not created"
    yield uid
    _exec("DELETE FROM users WHERE email=%s", (_EMAIL,))
    _exec("DELETE FROM auth_audit_log WHERE email=%s", (_EMAIL,))


@pytest.fixture
def gated_client():
    """A TestClient with the gate ACTIVE — the prod posture, which most tests run without."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from cqc_lem.api import main
    with patch.object(main, "_API_ACCESS_TOKEN_SET", {_TOKEN}), \
            TestClient(main.app, raise_server_exceptions=False) as client:
        yield client


# The route is deliberately an ordinary session-authenticated GET, not a special one: it resolves
# its caller exactly the way every other gated route does.
_GATED_ROUTE = "/api/user/security"


class TestTheCookieIsEnoughAndNothingLessIs:
    def test_a_real_session_cookie_reaches_a_gated_route_with_no_bearer(self, user_id, gated_client):
        token = db.create_session(user_id)
        gated_client.cookies.set(SESSION_COOKIE_NAME, token)
        resp = gated_client.get(_GATED_ROUTE)
        assert "authorization" not in {k.lower() for k in resp.request.headers}
        assert resp.status_code == 200, resp.text
        # The identity really came from the session, not from being let through the edge.
        assert resp.json()["detail"]["email"] == _EMAIL

    def test_a_forged_cookie_clears_the_edge_and_dies_in_the_handler(self, user_id, gated_client):
        gated_client.cookies.set(SESSION_COOKIE_NAME, "not-a-real-session-token")
        resp = gated_client.get(_GATED_ROUTE)
        assert resp.status_code == 401

    def test_no_credential_at_all_never_reaches_the_handler(self, user_id, gated_client):
        gated_client.cookies.clear()
        resp = gated_client.get(_GATED_ROUTE)
        assert resp.status_code == 401

    def test_the_non_browser_bearer_is_not_an_identity(self, user_id, gated_client):
        """It clears the edge filter and buys nothing: a bearer holder is not a signed-in user."""
        gated_client.cookies.clear()
        resp = gated_client.get(_GATED_ROUTE, headers={"Authorization": f"Bearer {_TOKEN}"})
        assert resp.status_code == 401

    def test_a_revoked_session_stops_working_immediately(self, user_id, gated_client):
        token = db.create_session(user_id)
        gated_client.cookies.set(SESSION_COOKIE_NAME, token)
        first = gated_client.get(_GATED_ROUTE)
        assert first.status_code == 200, first.text

        session_id = first.json()["detail"]["sessions"][0]["id"]
        assert db.revoke_session(user_id, session_id) is True
        assert gated_client.get(_GATED_ROUTE).status_code == 401
