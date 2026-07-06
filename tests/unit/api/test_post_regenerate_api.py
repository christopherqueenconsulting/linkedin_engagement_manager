"""Unit tests for POST /api/user/post/regenerate (regenerate-with-suggestions)."""

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


class TestRegeneratePostEndpoint:
    def test_dispatches_task_with_guidance(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_post_user_id", return_value=_USER), \
             patch("cqc_lem.app.run_content_plan.regenerate_post_task") as task:
            resp = client.post("/api/user/post/regenerate",
                               json={"session_token": _SESSION, "post_id": 7, "guidance": "punchier"})
        assert resp.status_code == 200
        task.apply_async.assert_called_once()
        kwargs = task.apply_async.call_args.kwargs["kwargs"]
        assert kwargs["post_id"] == 7 and kwargs["guidance"] == "punchier"

    def test_blank_guidance_becomes_none(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_post_user_id", return_value=_USER), \
             patch("cqc_lem.app.run_content_plan.regenerate_post_task") as task:
            resp = client.post("/api/user/post/regenerate",
                               json={"session_token": _SESSION, "post_id": 7, "guidance": "   "})
        assert resp.status_code == 200
        assert task.apply_async.call_args.kwargs["kwargs"]["guidance"] is None

    def test_404_when_not_owner(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch("cqc_lem.api.main.get_post_user_id", return_value=999), \
             patch("cqc_lem.app.run_content_plan.regenerate_post_task") as task:
            resp = client.post("/api/user/post/regenerate",
                               json={"session_token": _SESSION, "post_id": 7})
        assert resp.status_code == 404
        task.apply_async.assert_not_called()

    def test_401_when_no_session(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.post("/api/user/post/regenerate",
                               json={"session_token": "bad", "post_id": 7})
        assert resp.status_code == 401
