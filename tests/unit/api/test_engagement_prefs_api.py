"""Unit tests for the /api/user/engagement-preferences endpoints."""

import pytest
from unittest.mock import patch

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


class TestGetEngagementPreferences:
    def test_returns_prefs(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_engagement_preferences",
                   return_value={"tone": "warm", "comment_length": "short"}):
            resp = client.get(f"/api/user/engagement-preferences?session_token={_SESSION}")
        assert resp.status_code == 200
        assert resp.json()["detail"]["tone"] == "warm"

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.get("/api/user/engagement-preferences?session_token=bad")
        assert resp.status_code == 401


class TestUpdateEngagementPreferences:
    def test_updates_and_excludes_session_token(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION, "tone": "bold", "include_topics": ["AI"]})
        assert resp.status_code == 200
        prefs_arg = upd.call_args[0][1]
        assert "session_token" not in prefs_arg
        assert prefs_arg["tone"] == "bold" and prefs_arg["include_topics"] == ["AI"]

    def test_accepts_focus_and_goals(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=True) as upd:
            resp = client.put("/api/user/engagement-preferences",
                              json={"session_token": _SESSION, "focus_topics": ["B2B sales"],
                                    "business_goals": "book calls", "personal_goals": "grow authority"})
        assert resp.status_code == 200
        prefs_arg = upd.call_args[0][1]
        assert prefs_arg["focus_topics"] == ["B2B sales"]
        assert prefs_arg["business_goals"] == "book calls"
        assert prefs_arg["personal_goals"] == "grow authority"

    def test_500_on_failure(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_engagement_preferences", return_value=False):
            resp = client.put("/api/user/engagement-preferences", json={"session_token": _SESSION})
        assert resp.status_code == 500

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.put("/api/user/engagement-preferences", json={"session_token": "bad"})
        assert resp.status_code == 401
