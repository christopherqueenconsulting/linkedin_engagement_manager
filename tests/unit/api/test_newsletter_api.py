"""Unit tests for the /api/user/newsletter-settings endpoints."""

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


class TestNewsletterSettings:
    def test_get_returns_settings(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_newsletter_settings", return_value={"enabled": True, "cadence": "weekly"}):
            resp = client.get(f"/api/user/newsletter-settings?session_token={_SESSION}")
        assert resp.status_code == 200
        assert resp.json()["detail"]["cadence"] == "weekly"

    def test_put_updates(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.update_newsletter_settings", return_value=True) as upd:
            resp = client.put("/api/user/newsletter-settings", json={
                "session_token": _SESSION, "enabled": True, "title": "Weekly Wins", "cadence": "weekly"})
        assert resp.status_code == 200
        args = upd.call_args[0][1]
        assert "session_token" not in args and args["title"] == "Weekly Wins"

    def test_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.get("/api/user/newsletter-settings?session_token=bad")
        assert resp.status_code == 401
