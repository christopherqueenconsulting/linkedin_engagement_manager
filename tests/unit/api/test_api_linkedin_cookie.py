"""Unit tests for POST /api/user/linkedin-cookie."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


_SESSION = "tok_test"
_USER_ID = 42
_VALID = "AQEDAReallyLongLinkedInSessionTokenValue1234567890"


class TestStoreLinkedInCookie:
    def test_stores_valid_li_at(self, api_client, signed_in):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.routers.user.store_linkedin_li_at", return_value=True) as store:
            resp = api_client.post("/api/user/linkedin-cookie", json={
                "session_token": _SESSION, "li_at": _VALID,
            })
        assert resp.status_code == 200
        store.assert_called_once()
        assert store.call_args.args[0] == _USER_ID
        assert store.call_args.args[1] == _VALID

    def test_strips_surrounding_quotes(self, api_client, signed_in):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.routers.user.store_linkedin_li_at", return_value=True) as store:
            resp = api_client.post("/api/user/linkedin-cookie", json={
                "session_token": _SESSION, "li_at": f'  "{_VALID}"  ',
            })
        assert resp.status_code == 200
        assert store.call_args.args[1] == _VALID

    def test_passes_jsessionid_through(self, api_client, signed_in):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.routers.user.store_linkedin_li_at", return_value=True) as store:
            resp = api_client.post("/api/user/linkedin-cookie", json={
                "session_token": _SESSION, "li_at": _VALID, "jsessionid": "ajax:123",
            })
        assert resp.status_code == 200
        assert store.call_args.kwargs.get("jsessionid") == "ajax:123"

    def test_401_invalid_session(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = api_client.post("/api/user/linkedin-cookie", json={
                "session_token": "bad", "li_at": _VALID,
            })
        assert resp.status_code == 401

    @pytest.mark.parametrize("bad", ["short", "has space inside", "semi;colon", ""])
    def test_422_invalid_li_at(self, api_client, signed_in, bad):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.routers.user.store_linkedin_li_at", return_value=True) as store:
            resp = api_client.post("/api/user/linkedin-cookie", json={
                "session_token": _SESSION, "li_at": bad,
            })
        assert resp.status_code == 422
        store.assert_not_called()

    def test_500_when_store_fails(self, api_client, signed_in):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.routers.user.store_linkedin_li_at", return_value=False):
            resp = api_client.post("/api/user/linkedin-cookie", json={
                "session_token": _SESSION, "li_at": _VALID,
            })
        assert resp.status_code == 500


class TestCookieOnlyMigration:
    """Issue #745 §5.4 — the cookie replaces the stored password rather than sitting beside it."""

    def test_password_kept_by_default(self, api_client, signed_in):
        """The browser extension posts this body on every reconnect; it must never silently
        delete the user's password.
        """
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.routers.user.store_linkedin_li_at", return_value=True), \
             patch("cqc_lem.api.routers.user.clear_user_linkedin_password") as clear:
            resp = api_client.post("/api/user/linkedin-cookie", json={
                "session_token": _SESSION, "li_at": _VALID,
            })
        assert resp.status_code == 200
        clear.assert_not_called()

    def test_password_dropped_when_requested(self, api_client, signed_in):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.routers.user.store_linkedin_li_at", return_value=True), \
             patch("cqc_lem.api.routers.user.clear_user_linkedin_password", return_value=True) as clear:
            resp = api_client.post("/api/user/linkedin-cookie", json={
                "session_token": _SESSION, "li_at": _VALID, "drop_password": True,
            })
        assert resp.status_code == 200
        clear.assert_called_once_with(_USER_ID)
        assert "deleted" in resp.json()["detail"]

    def test_password_not_dropped_when_the_cookie_could_not_be_stored(self, api_client, signed_in):
        """Dropping it first would leave the account with no working login at all."""
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.routers.user.store_linkedin_li_at", return_value=False), \
             patch("cqc_lem.api.routers.user.clear_user_linkedin_password") as clear:
            resp = api_client.post("/api/user/linkedin-cookie", json={
                "session_token": _SESSION, "li_at": _VALID, "drop_password": True,
            })
        assert resp.status_code == 500
        clear.assert_not_called()
