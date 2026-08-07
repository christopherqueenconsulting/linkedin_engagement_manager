"""Unit tests for GET/PUT /api/user/timezone endpoints."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def client():
    patches = [
        patch("cqc_lem.utilities.observability.track_api_call"),
        patch("cqc_lem.app.run_automation.automate_invites_to_company_page_for_user"),
        patch("cqc_lem.app.run_automation.automate_reply_commenting"),
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


_BASE = "/api/user/timezone"
_SESSION = "tok_test"
_USER_ID = 42


class TestGetUserTimezone:
    def test_returns_timezone_for_valid_session(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.main.get_user_timezone", return_value="America/New_York"):
            resp = client.get(f"{_BASE}?session_token={_SESSION}")
        assert resp.status_code == 200
        assert resp.json()["detail"]["timezone"] == "America/New_York"

    def test_returns_401_for_invalid_session(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.get(f"{_BASE}?session_token=bad")
        assert resp.status_code == 401


class TestPutUserTimezone:
    def test_updates_valid_iana_timezone(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.main.update_user_timezone", return_value=True):
            resp = client.put(_BASE, json={"session_token": _SESSION, "timezone": "America/New_York"})
        assert resp.status_code == 200

    def test_rejects_invalid_timezone_string(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID):
            resp = client.put(_BASE, json={"session_token": _SESSION, "timezone": "Not/A/Timezone"})
        assert resp.status_code == 422

    def test_returns_401_for_invalid_session(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.put(_BASE, json={"session_token": "bad", "timezone": "UTC"})
        assert resp.status_code == 401

    def test_returns_500_when_db_update_fails(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.main.update_user_timezone", return_value=False):
            resp = client.put(_BASE, json={"session_token": _SESSION, "timezone": "UTC"})
        assert resp.status_code == 500
