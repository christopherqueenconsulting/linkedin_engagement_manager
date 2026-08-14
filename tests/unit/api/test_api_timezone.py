"""Unit tests for GET/PUT /api/user/timezone endpoints."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


_BASE = "/api/user/timezone"
_SESSION = "tok_test"
_USER_ID = 42


class TestGetUserTimezone:
    def test_returns_timezone_for_valid_session(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.routers.user.get_user_timezone", return_value="America/New_York"):
            resp = api_client.get(f"{_BASE}?session_token={_SESSION}")
        assert resp.status_code == 200
        assert resp.json()["detail"]["timezone"] == "America/New_York"

    def test_returns_401_for_invalid_session(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = api_client.get(f"{_BASE}?session_token=bad")
        assert resp.status_code == 401


class TestPutUserTimezone:
    def test_updates_valid_iana_timezone(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.routers.user.update_user_timezone", return_value=True):
            resp = api_client.put(_BASE, json={"session_token": _SESSION, "timezone": "America/New_York"})
        assert resp.status_code == 200

    def test_rejects_invalid_timezone_string(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID):
            resp = api_client.put(_BASE, json={"session_token": _SESSION, "timezone": "Not/A/Timezone"})
        assert resp.status_code == 422

    def test_returns_401_for_invalid_session(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = api_client.put(_BASE, json={"session_token": "bad", "timezone": "UTC"})
        assert resp.status_code == 401

    def test_returns_500_when_db_update_fails(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.routers.user.update_user_timezone", return_value=False):
            resp = api_client.put(_BASE, json={"session_token": _SESSION, "timezone": "UTC"})
        assert resp.status_code == 500
