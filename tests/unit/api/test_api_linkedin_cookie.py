"""Unit tests for POST /api/user/linkedin-cookie."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def client():
    patches = [
        patch("cqc_lem.utilities.observability.track_api_call"),
        patch("cqc_lem.app.engagement.invites.automate_invites_to_company_page_for_user"),
        patch("cqc_lem.app.engagement.posting.automate_reply_commenting"),
        patch("cqc_lem.app.run_content_plan.auto_create_weekly_content"),
        patch("cqc_lem.app.aws_test_celery_task.test_get_my_profile"),
    ]
    for p in patches:
        p.start()
    try:
        from fastapi.testclient import TestClient

        from cqc_lem.api.main import app
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc
    finally:
        for p in patches:
            p.stop()


_SESSION = "tok_test"
_USER_ID = 42
_VALID = "AQEDAReallyLongLinkedInSessionTokenValue1234567890"


class TestStoreLinkedInCookie:
    def test_stores_valid_li_at(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.routers.user.store_linkedin_li_at", return_value=True) as store:
            resp = client.post("/api/user/linkedin-cookie", json={
                "session_token": _SESSION, "li_at": _VALID,
            })
        assert resp.status_code == 200
        store.assert_called_once()
        assert store.call_args.args[0] == _USER_ID
        assert store.call_args.args[1] == _VALID

    def test_strips_surrounding_quotes(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.routers.user.store_linkedin_li_at", return_value=True) as store:
            resp = client.post("/api/user/linkedin-cookie", json={
                "session_token": _SESSION, "li_at": f'  "{_VALID}"  ',
            })
        assert resp.status_code == 200
        assert store.call_args.args[1] == _VALID

    def test_passes_jsessionid_through(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.routers.user.store_linkedin_li_at", return_value=True) as store:
            resp = client.post("/api/user/linkedin-cookie", json={
                "session_token": _SESSION, "li_at": _VALID, "jsessionid": "ajax:123",
            })
        assert resp.status_code == 200
        assert store.call_args.kwargs.get("jsessionid") == "ajax:123"

    def test_401_invalid_session(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.post("/api/user/linkedin-cookie", json={
                "session_token": "bad", "li_at": _VALID,
            })
        assert resp.status_code == 401

    @pytest.mark.parametrize("bad", ["short", "has space inside", "semi;colon", ""])
    def test_422_invalid_li_at(self, client, bad):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.routers.user.store_linkedin_li_at", return_value=True) as store:
            resp = client.post("/api/user/linkedin-cookie", json={
                "session_token": _SESSION, "li_at": bad,
            })
        assert resp.status_code == 422
        store.assert_not_called()

    def test_500_when_store_fails(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.routers.user.store_linkedin_li_at", return_value=False):
            resp = client.post("/api/user/linkedin-cookie", json={
                "session_token": _SESSION, "li_at": _VALID,
            })
        assert resp.status_code == 500


class TestCookieOnlyMigration:
    """Issue #745 §5.4 — the cookie replaces the stored password rather than sitting beside it."""

    def test_password_kept_by_default(self, client):
        """The browser extension posts this body on every reconnect; it must never silently
        delete the user's password.
        """
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.routers.user.store_linkedin_li_at", return_value=True), \
             patch("cqc_lem.api.routers.user.clear_user_linkedin_password") as clear:
            resp = client.post("/api/user/linkedin-cookie", json={
                "session_token": _SESSION, "li_at": _VALID,
            })
        assert resp.status_code == 200
        clear.assert_not_called()

    def test_password_dropped_when_requested(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.routers.user.store_linkedin_li_at", return_value=True), \
             patch("cqc_lem.api.routers.user.clear_user_linkedin_password", return_value=True) as clear:
            resp = client.post("/api/user/linkedin-cookie", json={
                "session_token": _SESSION, "li_at": _VALID, "drop_password": True,
            })
        assert resp.status_code == 200
        clear.assert_called_once_with(_USER_ID)
        assert "deleted" in resp.json()["detail"]

    def test_password_not_dropped_when_the_cookie_could_not_be_stored(self, client):
        """Dropping it first would leave the account with no working login at all."""
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.routers.user.store_linkedin_li_at", return_value=False), \
             patch("cqc_lem.api.routers.user.clear_user_linkedin_password") as clear:
            resp = client.post("/api/user/linkedin-cookie", json={
                "session_token": _SESSION, "li_at": _VALID, "drop_password": True,
            })
        assert resp.status_code == 500
        clear.assert_not_called()
