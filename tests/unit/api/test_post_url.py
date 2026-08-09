"""Unit tests for GET /api/post_url/ endpoint."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_DB = "cqc_lem.api.main"

from tests.unit.api.conftest import SESSION_TOKEN, SESSION_USER_ID  # noqa: E402


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


_URL = "/api/post_url/"
_POST_ID = 42
_LI_URL = "https://www.linkedin.com/feed/update/urn:li:ugcPost:123/"


class TestGetPostUrl:
    def test_no_session_returns_401(self, client):
        with patch(f"{_DB}.get_session_user_id", return_value=None):
            resp = client.get(_URL, params={"post_id": _POST_ID})
        assert resp.status_code == 401

    def test_another_accounts_email_returns_403(self, client, signed_in):
        with patch(f"{_DB}.get_post_url_from_log_for_user") as mock_url:
            resp = client.get(_URL, params={"post_id": _POST_ID,
                                            "session_token": SESSION_TOKEN,
                                            "email": "victim@example.com"})
        assert resp.status_code == 403
        mock_url.assert_not_called()

    def test_missing_post_id_returns_422(self, client, signed_in):
        resp = client.get(_URL, params={"session_token": SESSION_TOKEN})
        assert resp.status_code == 422

    def test_returns_linkedin_url_when_found(self, client, signed_in):
        with patch(f"{_DB}.get_post_url_from_log_for_user", return_value=_LI_URL):
            resp = client.get(_URL, params={"post_id": _POST_ID, "session_token": SESSION_TOKEN})
        assert resp.status_code == 200
        assert resp.json()["detail"]["post_url"] == _LI_URL

    def test_returns_null_when_no_log_url_exists(self, client, signed_in):
        with patch(f"{_DB}.get_post_url_from_log_for_user", return_value=None):
            resp = client.get(_URL, params={"post_id": _POST_ID, "session_token": SESSION_TOKEN})
        assert resp.status_code == 200
        assert resp.json()["detail"]["post_url"] is None

    def test_lookup_is_scoped_to_the_session_user(self, client, signed_in):
        """A foreign post_id reads as "no URL" — the query never leaves the caller's own logs."""
        with patch(f"{_DB}.get_post_url_from_log_for_user", return_value=None) as mock_url:
            client.get(_URL, params={"post_id": _POST_ID, "session_token": SESSION_TOKEN})
        mock_url.assert_called_once_with(SESSION_USER_ID, _POST_ID)
