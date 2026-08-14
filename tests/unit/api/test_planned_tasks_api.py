"""Unit tests for the /api/dashboard/planned-tasks/ endpoint."""

import datetime
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

from tests.unit.api.conftest import SESSION_TOKEN, SESSION_USER_ID  # noqa: E402


class TestPlannedTasksEndpoint:
    BASE = "/api/dashboard/planned-tasks/"

    def test_returns_labeled_upcoming_tasks(self, api_client, signed_in):
        tasks = [
            {"kind": "DM", "id": 5, "title": "Alice", "status": "pending",
             "scheduled_time": datetime.datetime(2026, 7, 10, 9, 0)},
            {"kind": "Newsletter", "id": 9, "title": "Weekly", "status": "draft",
             "scheduled_time": datetime.datetime(2026, 7, 11, 9, 0)},
            {"kind": "Post", "id": 1, "title": "Body", "status": "approved",
             "scheduled_time": datetime.datetime(2026, 7, 12, 9, 0)},
        ]
        with patch("cqc_lem.api.main.get_planned_tasks", return_value=tasks) as gpt:
            resp = api_client.get(f"{self.BASE}?session_token={SESSION_TOKEN}")
        assert resp.status_code == 200
        out = resp.json()["detail"]["tasks"]
        assert [t["kind"] for t in out] == ["DM", "Newsletter", "Post"]
        # scheduled_time serialized as ISO string, no 'posted'/'sent'/'published' terminal states
        assert isinstance(out[0]["scheduled_time"], str)
        assert all(t["status"] not in ("posted", "sent", "published") for t in out)
        assert gpt.call_args.args[0] == SESSION_USER_ID

    def test_no_session_401(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = api_client.get(self.BASE)
        assert resp.status_code == 401

    def test_another_accounts_email_403(self, api_client, signed_in):
        with patch("cqc_lem.api.main.get_planned_tasks") as gpt:
            resp = api_client.get(f"{self.BASE}?session_token={SESSION_TOKEN}&email=victim@example.com")
        assert resp.status_code == 403
        gpt.assert_not_called()

    def test_limit_forwarded(self, api_client, signed_in):
        with patch("cqc_lem.api.main.get_planned_tasks", return_value=[]) as gpt:
            resp = api_client.get(f"{self.BASE}?session_token={SESSION_TOKEN}&limit=3")
        assert resp.status_code == 200
        assert gpt.call_args.kwargs.get("limit") == 3
