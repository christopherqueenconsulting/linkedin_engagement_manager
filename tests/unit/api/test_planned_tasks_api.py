"""Unit tests for the /api/dashboard/planned-tasks/ endpoint."""

import datetime
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


_EMAIL = "u@example.com"


class TestPlannedTasksEndpoint:
    def test_returns_labeled_upcoming_tasks(self, client):
        tasks = [
            {"kind": "DM", "id": 5, "title": "Alice", "status": "pending",
             "scheduled_time": datetime.datetime(2026, 7, 10, 9, 0)},
            {"kind": "Newsletter", "id": 9, "title": "Weekly", "status": "draft",
             "scheduled_time": datetime.datetime(2026, 7, 11, 9, 0)},
            {"kind": "Post", "id": 1, "title": "Body", "status": "approved",
             "scheduled_time": datetime.datetime(2026, 7, 12, 9, 0)},
        ]
        with patch("cqc_lem.api.main.get_user_id", return_value=42), \
             patch("cqc_lem.api.main.get_planned_tasks", return_value=tasks) as gpt:
            resp = client.get(f"/api/dashboard/planned-tasks/?email={_EMAIL}")
        assert resp.status_code == 200
        out = resp.json()["detail"]["tasks"]
        assert [t["kind"] for t in out] == ["DM", "Newsletter", "Post"]
        # scheduled_time serialized as ISO string, no 'posted'/'sent'/'published' terminal states
        assert isinstance(out[0]["scheduled_time"], str)
        assert all(t["status"] not in ("posted", "sent", "published") for t in out)
        gpt.assert_called_once()

    def test_missing_email_400(self, client):
        resp = client.get("/api/dashboard/planned-tasks/?email=")
        assert resp.status_code == 400

    def test_unknown_user_403(self, client):
        with patch("cqc_lem.api.main.get_user_id", return_value=None):
            resp = client.get(f"/api/dashboard/planned-tasks/?email={_EMAIL}")
        assert resp.status_code == 403

    def test_limit_forwarded(self, client):
        with patch("cqc_lem.api.main.get_user_id", return_value=42), \
             patch("cqc_lem.api.main.get_planned_tasks", return_value=[]) as gpt:
            resp = client.get(f"/api/dashboard/planned-tasks/?email={_EMAIL}&limit=3")
        assert resp.status_code == 200
        assert gpt.call_args.kwargs.get("limit") == 3
