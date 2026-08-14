"""Unit tests for GET /api/post_url/ endpoint."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_DB = "cqc_lem.api.main"

from tests.unit.api.conftest import SESSION_TOKEN, SESSION_USER_ID  # noqa: E402

_URL = "/api/post_url/"
_POST_ID = 42
_LI_URL = "https://www.linkedin.com/feed/update/urn:li:ugcPost:123/"


class TestGetPostUrl:
    def test_no_session_returns_401(self, api_client):
        with patch(f"{_DB}.get_session_user_id", return_value=None):
            resp = api_client.get(_URL, params={"post_id": _POST_ID})
        assert resp.status_code == 401

    def test_another_accounts_email_returns_403(self, api_client, signed_in):
        with patch(f"{_DB}.get_post_url_from_log_for_user") as mock_url:
            resp = api_client.get(_URL, params={"post_id": _POST_ID,
                                            "session_token": SESSION_TOKEN,
                                            "email": "victim@example.com"})
        assert resp.status_code == 403
        mock_url.assert_not_called()

    def test_missing_post_id_returns_422(self, api_client, signed_in):
        resp = api_client.get(_URL, params={"session_token": SESSION_TOKEN})
        assert resp.status_code == 422

    def test_returns_linkedin_url_when_found(self, api_client, signed_in):
        with patch(f"{_DB}.get_post_url_from_log_for_user", return_value=_LI_URL):
            resp = api_client.get(_URL, params={"post_id": _POST_ID, "session_token": SESSION_TOKEN})
        assert resp.status_code == 200
        assert resp.json()["detail"]["post_url"] == _LI_URL

    def test_returns_null_when_no_log_url_exists(self, api_client, signed_in):
        with patch(f"{_DB}.get_post_url_from_log_for_user", return_value=None):
            resp = api_client.get(_URL, params={"post_id": _POST_ID, "session_token": SESSION_TOKEN})
        assert resp.status_code == 200
        assert resp.json()["detail"]["post_url"] is None

    def test_lookup_is_scoped_to_the_session_user(self, api_client, signed_in):
        """A foreign post_id reads as "no URL" — the query never leaves the caller's own logs."""
        with patch(f"{_DB}.get_post_url_from_log_for_user", return_value=None) as mock_url:
            api_client.get(_URL, params={"post_id": _POST_ID, "session_token": SESSION_TOKEN})
        mock_url.assert_called_once_with(SESSION_USER_ID, _POST_ID)
