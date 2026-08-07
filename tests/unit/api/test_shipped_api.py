"""Unit tests for the shipped-notice endpoints (issue #502)."""

from datetime import datetime
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_DB = "cqc_lem.utilities.db"
_S = "cqc_lem.utilities.surveys"

_NOTICE = {"id": 11, "github_issue_number": 498, "pr_number": 539, "title": "fix: x",
           "changelog_line": "**Fixed:** Comments post (#498, PR #539)",
           "shipped_at": datetime(2026, 7, 25, 10, 0), "notified_at": datetime(2026, 7, 25, 10, 5)}


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


class TestShippedNoticesEndpoint:
    def test_returns_the_users_notices_and_the_changelog(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_DB}.get_unseen_shipped_notices", return_value=[_NOTICE]) as unseen, \
             patch(f"{_DB}.get_recent_shipped_notices", return_value=[_NOTICE]), \
             patch("cqc_lem.utilities.feedback.shipped.fix_csat_delay_hours", return_value=24):
            resp = client.get("/api/user/shipped?session_token=tok")
        assert resp.status_code == 200
        detail = resp.json()["detail"]
        assert detail["notices"] == [{
            "id": 11, "issue_number": 498,
            "changelog_line": "**Fixed:** Comments post (#498, PR #539)",
            "shipped_at": "2026-07-25T10:00:00"}]
        assert detail["changelog"][0]["issue_number"] == 498
        # The configured delay is what schedules the micro-CSAT the notice carries.
        assert unseen.call_args.kwargs["delay_hours"] == 24

    def test_requires_a_valid_session(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = client.get("/api/user/shipped?session_token=bad")
        assert resp.status_code == 401


class TestAckEndpoint:
    def test_records_the_micro_csat_and_marks_the_notice_seen(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_DB}.get_unseen_shipped_notices", return_value=[_NOTICE]) as unseen, \
             patch(f"{_DB}.mark_shipped_notice_seen", return_value=True) as seen, \
             patch("cqc_lem.utilities.feedback.shipped.fix_csat_delay_hours", return_value=24), \
             patch(f"{_S}.record_fix_csat_response", return_value=77) as record:
            resp = client.post("/api/shipped/ack", json={
                "session_token": "tok", "notice_id": 11, "resolved": False,
                "comment": "still broken"})
        assert resp.status_code == 200
        assert resp.json()["detail"] == {"acknowledged": True, "feedback_id": 77}
        seen.assert_called_once_with(11, 42)
        assert record.call_args.args == (42, 498, False)
        assert record.call_args.kwargs["comment"] == "still broken"
        # Same delay gate as the GET — an ack can't outrun the notice it acknowledges.
        assert unseen.call_args.kwargs["delay_hours"] == 24

    def test_a_notice_still_inside_the_csat_delay_is_not_ackable(self, client):
        """The delay gate is applied on ack too: acking early would consume the notice (and its
        micro-CSAT) before the user was ever shown it.
        """
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_DB}.get_unseen_shipped_notices", return_value=[]) as unseen, \
             patch(f"{_DB}.mark_shipped_notice_seen") as seen, \
             patch("cqc_lem.utilities.feedback.shipped.fix_csat_delay_hours", return_value=24), \
             patch(f"{_S}.record_fix_csat_response") as record:
            resp = client.post("/api/shipped/ack", json={
                "session_token": "tok", "notice_id": 11, "resolved": True})
        assert resp.status_code == 404
        assert unseen.call_args.kwargs["delay_hours"] == 24
        seen.assert_not_called()
        record.assert_not_called()

    def test_dismissal_without_an_answer_records_no_feedback(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_DB}.get_unseen_shipped_notices", return_value=[_NOTICE]), \
             patch(f"{_DB}.mark_shipped_notice_seen", return_value=True), \
             patch(f"{_S}.record_fix_csat_response") as record:
            resp = client.post("/api/shipped/ack",
                               json={"session_token": "tok", "notice_id": 11})
        assert resp.json()["detail"] == {"acknowledged": True, "feedback_id": None}
        record.assert_not_called()

    def test_a_notice_addressed_to_someone_else_is_not_ackable(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=42), \
             patch(f"{_DB}.get_unseen_shipped_notices", return_value=[_NOTICE]), \
             patch(f"{_DB}.mark_shipped_notice_seen") as seen, \
             patch(f"{_S}.record_fix_csat_response") as record:
            resp = client.post("/api/shipped/ack",
                               json={"session_token": "tok", "notice_id": 999, "resolved": True})
        assert resp.status_code == 404
        seen.assert_not_called()
        record.assert_not_called()

    def test_requires_a_valid_session(self, client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            resp = client.post("/api/shipped/ack",
                               json={"session_token": "bad", "notice_id": 11})
        assert resp.status_code == 401
