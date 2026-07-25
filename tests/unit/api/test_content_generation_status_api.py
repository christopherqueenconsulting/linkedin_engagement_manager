"""Unit tests for GET /api/content_generation_status/ and the queued-on-dispatch progress
record written by POST /api/create_weekly_content/ (issue #545)."""

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
_USER = 7
_STATUS = {
    "state": "in_progress",
    "total": 3,
    "completed": 1,
    "failed": 0,
    "post_ids": [1, 2, 3],
    "ready_post_ids": [1],
    "failed_post_ids": [],
    "started_at": "2026-07-25T10:00:00+00:00",
    "finished_at": None,
}


class TestContentGenerationStatusEndpoint:
    def test_returns_status_for_the_session_user(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_generation_status", return_value=_STATUS) as get_status:
            resp = client.get("/api/content_generation_status/",
                              params={"session_token": _SESSION})
        assert resp.status_code == 200
        assert resp.json()["detail"] == _STATUS
        get_status.assert_called_once_with(_USER)

    def test_returns_null_when_no_run_tracked(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_generation_status", return_value=None):
            resp = client.get("/api/content_generation_status/",
                              params={"session_token": _SESSION})
        assert resp.status_code == 200
        assert resp.json()["detail"] is None

    def test_rejects_invalid_session(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None), \
             patch("cqc_lem.api.main.get_generation_status") as get_status:
            resp = client.get("/api/content_generation_status/", params={"session_token": "bad"})
        assert resp.status_code == 401
        get_status.assert_not_called()

    def test_requires_a_session_token(self, client):
        resp = client.get("/api/content_generation_status/")
        assert resp.status_code == 422


class TestCreateWeeklyContentMarksQueued:
    def test_marks_queued_before_dispatch(self, client):
        with patch("cqc_lem.api.main.mark_queued") as queued, \
             patch("cqc_lem.api.main.celery_chain") as chain:
            resp = client.post("/api/create_weekly_content/", params={"user_id": _USER})
        assert resp.status_code == 200
        queued.assert_called_once_with(_USER)
        chain.return_value.apply_async.assert_called_once()

    def test_missing_user_id_does_not_mark_queued(self, client):
        with patch("cqc_lem.api.main.mark_queued") as queued:
            resp = client.post("/api/create_weekly_content/", params={"user_id": 0})
        assert resp.status_code == 400
        queued.assert_not_called()

    def test_dispatch_failure_clears_the_queued_record(self, client):
        """A broker outage must not strand the SPA polling a run that will never start."""
        with patch("cqc_lem.api.main.mark_queued"), \
             patch("cqc_lem.api.main.clear_generation_status") as cleared, \
             patch("cqc_lem.api.main.celery_chain") as chain:
            chain.return_value.apply_async.side_effect = OSError("broker unreachable")
            resp = client.post("/api/create_weekly_content/", params={"user_id": _USER})
        assert resp.status_code == 500
        cleared.assert_called_once_with(_USER)
