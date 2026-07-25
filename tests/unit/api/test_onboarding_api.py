"""Unit tests for GET /api/user/onboarding (issue #500)."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"


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


_SNAPSHOT = {
    "activated": False,
    "started_at": "2026-07-20T09:00:00",
    "steps": [
        {"key": "linkedin_connected", "label": "Connect LinkedIn", "hint": "", "path": "/account",
         "ok": True, "completed_at": "2026-07-21T09:00:00"},
        {"key": "voice_set", "label": "Set your voice & targeting", "hint": "", "path": "/account",
         "ok": False, "completed_at": None},
    ],
    "nudge": {"key": "set_voice", "headline": "Set your voice", "body": "Pick your tone.",
              "cta_label": "Set your voice", "cta_path": "/account"},
}


class TestOnboardingEndpoint:
    def test_returns_the_checklist_and_nudge(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch("cqc_lem.utilities.onboarding.onboarding_snapshot",
                   return_value=_SNAPSHOT) as snap:
            resp = client.get("/api/user/onboarding?session_token=tok")
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["activated"] is False
        assert [s["key"] for s in detail["steps"]] == ["linkedin_connected", "voice_set"]
        assert detail["nudge"]["key"] == "set_voice"
        snap.assert_called_once_with(42)

    def test_401_invalid_session(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = client.get("/api/user/onboarding?session_token=bad")
        assert resp.status_code == 401
