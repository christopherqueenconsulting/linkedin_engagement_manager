"""Unit tests for the /api/user/dm-templates endpoints."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


_SESSION = "tok"
_USER = 5


class TestGetDmTemplates:
    def test_returns_list(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.get_dm_templates", return_value=[{"event_type": "manual", "step": 0}]):
            resp = api_client.get(f"/api/user/dm-templates?session_token={_SESSION}")
        assert resp.status_code == 200
        assert resp.json()["detail"][0]["event_type"] == "manual"

    def test_401(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = api_client.get("/api/user/dm-templates?session_token=bad")
        assert resp.status_code == 401


class TestUpdateDmTemplates:
    def test_upserts(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.upsert_dm_templates", return_value=True) as upd:
            resp = api_client.put("/api/user/dm-templates", json={
                "session_token": _SESSION,
                "templates": [{"event_type": "connection_accepted", "step": 0,
                               "delay_hours": 0, "template_text": "Hi {first_name}", "is_active": True}]})
        assert resp.status_code == 200
        assert upd.call_args[0][1][0]["event_type"] == "connection_accepted"

    def test_500_on_failure(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.routers.user.upsert_dm_templates", return_value=False):
            resp = api_client.put("/api/user/dm-templates", json={"session_token": _SESSION, "templates": []})
        assert resp.status_code == 500

    def test_401(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = api_client.put("/api/user/dm-templates", json={"session_token": "bad", "templates": []})
        assert resp.status_code == 401
