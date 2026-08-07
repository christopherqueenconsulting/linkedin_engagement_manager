"""Unit tests for the /api/user/dm-templates endpoints."""

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


_SESSION = "tok"
_USER = 5


class TestGetDmTemplates:
    def test_returns_list(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_dm_templates", return_value=[{"event_type": "manual", "step": 0}]):
            resp = client.get(f"/api/user/dm-templates?session_token={_SESSION}")
        assert resp.status_code == 200
        assert resp.json()["detail"][0]["event_type"] == "manual"

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.get("/api/user/dm-templates?session_token=bad")
        assert resp.status_code == 401


class TestUpdateDmTemplates:
    def test_upserts(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.upsert_dm_templates", return_value=True) as upd:
            resp = client.put("/api/user/dm-templates", json={
                "session_token": _SESSION,
                "templates": [{"event_type": "connection_accepted", "step": 0,
                               "delay_hours": 0, "template_text": "Hi {first_name}", "is_active": True}]})
        assert resp.status_code == 200
        assert upd.call_args[0][1][0]["event_type"] == "connection_accepted"

    def test_500_on_failure(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.upsert_dm_templates", return_value=False):
            resp = client.put("/api/user/dm-templates", json={"session_token": _SESSION, "templates": []})
        assert resp.status_code == 500

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.put("/api/user/dm-templates", json={"session_token": "bad", "templates": []})
        assert resp.status_code == 401
