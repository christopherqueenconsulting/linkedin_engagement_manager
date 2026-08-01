"""Unit tests for the feedback admin triage panel endpoints (issue #793)."""

import json
import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _auth_hardening_side_effects():
    """Issue #745 (2b): every login now stamps `email_verified_at`, writes an `auth_audit_log` row
    and reads the PIN lockout, and /auth/session resolves the account's public_uid. Those are DB
    calls these tests never mocked — pin them so each test still exercises the flow it was written
    for. The hardening itself has its own suite (tests/unit/api/test_auth_hardening.py)."""
    with patch("cqc_lem.api.main.record_auth_event", return_value=True), \
         patch("cqc_lem.api.main.mark_email_verified", return_value=True), \
         patch("cqc_lem.api.main.get_pin_lockout", return_value=None), \
         patch("cqc_lem.api.main.get_user_public_uid", return_value="pub-uid-1"):
        yield


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


_ADMIN_USER = {"id": 7, "email": "admin@example.com", "is_admin": True}
_NON_ADMIN_USER = {"id": 8, "email": "user@example.com", "is_admin": False}


def _auth(user):
    return {
        "get_session": patch("cqc_lem.api.main.get_session_user_id", return_value=user["id"]),
        "is_admin": patch("cqc_lem.api.main.is_user_admin", return_value=user["is_admin"]),
        "get_email": patch("cqc_lem.api.main.get_user_email", return_value=user["email"]),
    }


class TestListFeedback:
    def test_forbidden_for_non_admin(self, client):
        with _auth(_NON_ADMIN_USER)["get_session"], _auth(_NON_ADMIN_USER)["is_admin"]:
            r = client.get("/api/admin/feedback", params={"session_token": "tok"})
        assert r.status_code == 403

    def test_401_for_invalid_session(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            r = client.get("/api/admin/feedback", params={"session_token": "bad"})
        assert r.status_code == 401

    def test_returns_items_for_admin(self, client):
        row = {
            "id": 1, "user_id": 2, "email": "user@x.com", "is_admin": 0,
            "source": "widget", "type_hint": "bug", "body": "broken",
            "context_json": json.dumps({"route": "/"}), "status": "new",
            "cluster_id": None, "github_issue_number": None,
            "reviewed_by": None, "reviewed_at": None,
            "created_at": "2026-07-29T12:00:00Z",
        }
        with _auth(_ADMIN_USER)["get_session"], _auth(_ADMIN_USER)["is_admin"], \
             patch("cqc_lem.api.main.get_feedback_list", return_value=[row]) as lister:
            r = client.get("/api/admin/feedback", params={
                "session_token": "tok", "status": "new", "source": "widget",
                "limit": 10, "offset": 5,
            })
        assert r.status_code == 200
        assert r.json()["detail"]["items"][0]["email"] == "user@x.com"
        assert r.json()["detail"]["items"][0]["is_admin_reporter"] is False
        lister.assert_called_once_with(status="new", source="widget", limit=10, offset=5)

    def test_no_session_param_is_422(self, client):
        with _auth(_ADMIN_USER)["get_session"], _auth(_ADMIN_USER)["is_admin"]:
            r = client.get("/api/admin/feedback")
        assert r.status_code == 422


class TestReviewFeedback:
    def test_dismiss_action_for_admin(self, client):
        with _auth(_ADMIN_USER)["get_session"], _auth(_ADMIN_USER)["is_admin"], \
             patch("cqc_lem.api.main.get_feedback_by_id", return_value={"id": 3}), \
             patch("cqc_lem.api.main.record_feedback_review", return_value=True) as recorder:
            r = client.post("/api/admin/feedback/3/review", json={
                "session_token": "tok", "action": "dismiss",
            })
        assert r.status_code == 200
        assert r.json()["detail"]["action"] == "dismissed"
        recorder.assert_called_once_with(3, 7, status="dismissed")

    def test_approve_action_runs_filer_and_records_reviewer(self, client):
        with _auth(_ADMIN_USER)["get_session"], _auth(_ADMIN_USER)["is_admin"], \
             patch("cqc_lem.api.main.get_feedback_by_id", return_value={"id": 5}), \
             patch("cqc_lem.utilities.feedback.issue_service.file_feedback_issue",
                   return_value={"action": "filed", "issue_number": 101}) as filer, \
             patch("cqc_lem.api.main.record_feedback_review", return_value=True) as recorder:
            r = client.post("/api/admin/feedback/5/review", json={
                "session_token": "tok", "action": "approve",
            })
        assert r.status_code == 200
        assert r.json()["detail"]["filing_result"]["issue_number"] == 101
        filer.assert_called_once_with({"id": 5})
        recorder.assert_called_once_with(5, 7)

    def test_already_filed_row_cannot_be_re_approved(self, client):
        """A filed row IS its own open cluster, so re-running the filer would match it to itself
        and post a false "+1 another report" on the issue it created."""
        row = {"id": 9, "status": "issue_created", "github_issue_number": 404}
        with _auth(_ADMIN_USER)["get_session"], _auth(_ADMIN_USER)["is_admin"], \
             patch("cqc_lem.api.main.get_feedback_by_id", return_value=row), \
             patch("cqc_lem.utilities.feedback.issue_service.file_feedback_issue") as filer, \
             patch("cqc_lem.api.main.record_feedback_review") as recorder:
            r = client.post("/api/admin/feedback/9/review", json={
                "session_token": "tok", "action": "approve",
            })
        assert r.status_code == 409
        filer.assert_not_called()
        recorder.assert_not_called()

    def test_already_clustered_row_cannot_be_dismissed(self, client):
        row = {"id": 10, "status": "clustered", "github_issue_number": None}
        with _auth(_ADMIN_USER)["get_session"], _auth(_ADMIN_USER)["is_admin"], \
             patch("cqc_lem.api.main.get_feedback_by_id", return_value=row), \
             patch("cqc_lem.api.main.record_feedback_review") as recorder:
            r = client.post("/api/admin/feedback/10/review", json={
                "session_token": "tok", "action": "dismiss",
            })
        assert r.status_code == 409
        recorder.assert_not_called()

    def test_new_row_is_still_reviewable(self, client):
        row = {"id": 11, "status": "new", "github_issue_number": None}
        with _auth(_ADMIN_USER)["get_session"], _auth(_ADMIN_USER)["is_admin"], \
             patch("cqc_lem.api.main.get_feedback_by_id", return_value=row), \
             patch("cqc_lem.api.main.record_feedback_review", return_value=True):
            r = client.post("/api/admin/feedback/11/review", json={
                "session_token": "tok", "action": "dismiss",
            })
        assert r.status_code == 200

    def test_404_when_feedback_row_missing(self, client):
        with _auth(_ADMIN_USER)["get_session"], _auth(_ADMIN_USER)["is_admin"], \
             patch("cqc_lem.api.main.get_feedback_by_id", return_value=None):
            r = client.post("/api/admin/feedback/99/review", json={
                "session_token": "tok", "action": "dismiss",
            })
        assert r.status_code == 404

    def test_forbidden_for_non_admin(self, client):
        with _auth(_NON_ADMIN_USER)["get_session"], _auth(_NON_ADMIN_USER)["is_admin"]:
            r = client.post("/api/admin/feedback/1/review", json={
                "session_token": "tok", "action": "dismiss",
            })
        assert r.status_code == 403

    def test_invalid_action_is_422(self, client):
        with _auth(_ADMIN_USER)["get_session"], _auth(_ADMIN_USER)["is_admin"]:
            r = client.post("/api/admin/feedback/1/review", json={
                "session_token": "tok", "action": "banish",
            })
        assert r.status_code == 422

    def test_missing_session_is_422(self, client):
        with _auth(_ADMIN_USER)["get_session"], _auth(_ADMIN_USER)["is_admin"]:
            r = client.post("/api/admin/feedback/1/review", json={"action": "dismiss"})
        assert r.status_code == 422


class TestSessionExposesAdminFlag:
    def test_auth_session_includes_is_admin(self, client):
        profile = {
            "subscription_tier": "professional",
            "subscription_status": "active",
            "timezone": "UTC",
            "created_at": None,
            "onboarding_completed_at": None,
            "posts_approved": 0,
        }
        with patch("cqc_lem.api.main.get_session_user_id", return_value=7), \
             patch("cqc_lem.api.main.get_user_email", return_value="admin@x.com"), \
             patch("cqc_lem.api.main.get_user_analytics_profile", return_value=profile), \
             patch("cqc_lem.api.main.is_user_admin", return_value=True):
            r = client.get("/api/auth/session", params={"session_token": "tok"})
        assert r.status_code == 200
        assert r.json()["detail"]["is_admin"] is True
