"""Unit tests for the feedback admin triage panel endpoints (issues #793, #1070)."""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from cqc_lem.utilities.feedback.classifier import (
    FeedbackCategory,
    FeedbackClassification,
    FeedbackRisk,
    FeedbackSeverity,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _auth_hardening_side_effects():
    """Issue #745 (2b): every login now stamps `email_verified_at`, writes an `auth_audit_log` row.

    It also reads the PIN lockout, and /auth/session resolves the account's public_uid. Those are
    DB calls these tests never mocked — pin them so each test still exercises the flow it was
    written for. The hardening itself has its own suite (tests/unit/api/test_auth_hardening.py).
    """
    with patch("cqc_lem.api.main.record_auth_event", return_value=True), \
         patch("cqc_lem.api.routers.auth.record_auth_event", return_value=True), \
         patch("cqc_lem.api.routers.auth.mark_email_verified", return_value=True), \
         patch("cqc_lem.api.routers.auth.get_pin_lockout", return_value=None), \
         patch("cqc_lem.api.routers.auth.get_user_public_uid", return_value="pub-uid-1"):
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
    """Patch targets for the `/api/admin/feedback` routes, which moved to their own router (#1154).

    `is_user_admin` moved with `_require_user_admin` and is read from the ROUTER's globals;
    `get_session_user_id` is the auth kernel, which stays in `main` and is reached as a host-module
    attribute at request time — so patching it there is correct and must stay that way.

    `/api/auth/session` reports the same admin flag, but off `cqc_lem.api.routers.auth` since #1154,
    and `TestSessionExposesAdminFlag` patches it there itself. Nothing here speaks for that route.
    """
    return {
        "get_session": patch("cqc_lem.api.main.get_session_user_id", return_value=user["id"]),
        "is_admin": patch("cqc_lem.api.routers.admin.is_user_admin", return_value=user["is_admin"]),
    }


def _classification():
    """A confident bug classification that routes to AUTO_WORK."""
    return FeedbackClassification(
        category=FeedbackCategory.BUG,
        severity=FeedbackSeverity.HIGH,
        component="ui",
        title="Fix the thing",
        summary="Thing is broken",
        risk=FeedbackRisk.NONE,
        confidence=0.9,
    )


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
             patch("cqc_lem.api.routers.admin.get_feedback_list", return_value=[row]) as lister:
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
             patch("cqc_lem.api.routers.admin.get_feedback_by_id", return_value={"id": 3}), \
             patch("cqc_lem.api.routers.admin.record_feedback_review", return_value=True) as recorder:
            r = client.post("/api/admin/feedback/3/review", json={
                "session_token": "tok", "action": "dismiss",
            })
        assert r.status_code == 200
        assert r.json()["detail"]["action"] == "dismissed"
        recorder.assert_called_once_with(3, 7, status="dismissed")

    def test_approve_action_runs_filer_and_records_reviewer(self, client):
        with _auth(_ADMIN_USER)["get_session"], _auth(_ADMIN_USER)["is_admin"], \
             patch("cqc_lem.api.routers.admin.get_feedback_by_id", return_value={"id": 5}), \
             patch("cqc_lem.utilities.feedback.issue_service.file_feedback_issue",
                   return_value={"action": "filed", "issue_number": 101}) as filer, \
             patch("cqc_lem.api.routers.admin.record_feedback_review", return_value=True) as recorder:
            r = client.post("/api/admin/feedback/5/review", json={
                "session_token": "tok", "action": "approve",
            })
        assert r.status_code == 200
        assert r.json()["detail"]["filing_result"]["issue_number"] == 101
        assert r.json()["detail"]["filed"] is True
        # Issue #1036: the filer must be told a human is watching, or it re-applies the
        # unattended-run holds and leaves the row exactly where the admin found it.
        filer.assert_called_once_with({"id": 5}, admin_approved=True)
        recorder.assert_called_once_with(5, 7)

    def test_deduped_approve_still_counts_as_filed(self, client):
        with _auth(_ADMIN_USER)["get_session"], _auth(_ADMIN_USER)["is_admin"], \
             patch("cqc_lem.api.routers.admin.get_feedback_by_id", return_value={"id": 6}), \
             patch("cqc_lem.utilities.feedback.issue_service.file_feedback_issue",
                   return_value={"action": "deduped", "issue_number": 77}), \
             patch("cqc_lem.api.routers.admin.record_feedback_review", return_value=True):
            r = client.post("/api/admin/feedback/6/review", json={
                "session_token": "tok", "action": "approve",
            })
        assert r.json()["detail"]["filed"] is True

    def test_approve_that_reached_no_issue_says_so(self, client):
        """The 200 only means the review was recorded.

        A GitHub failure changes NOTHING else, so without `filed` the panel cannot tell it apart
        from a successful approve (issue #1036).
        """
        with _auth(_ADMIN_USER)["get_session"], _auth(_ADMIN_USER)["is_admin"], \
             patch("cqc_lem.api.routers.admin.get_feedback_by_id", return_value={"id": 12}), \
             patch("cqc_lem.utilities.feedback.issue_service.file_feedback_issue",
                   return_value={"action": "error", "reason": "issue creation failed"}), \
             patch("cqc_lem.api.routers.admin.record_feedback_review", return_value=True):
            r = client.post("/api/admin/feedback/12/review", json={
                "session_token": "tok", "action": "approve",
            })
        assert r.status_code == 200
        assert r.json()["detail"]["filed"] is False
        assert r.json()["detail"]["filing_result"]["action"] == "error"

    def test_already_filed_row_cannot_be_re_approved(self, client):
        """A filed row IS its own open cluster.

        Re-running the filer would match it to itself and post a false "+1 another report" on the
        issue it created.
        """
        row = {"id": 9, "status": "issue_created", "github_issue_number": 404}
        with _auth(_ADMIN_USER)["get_session"], _auth(_ADMIN_USER)["is_admin"], \
             patch("cqc_lem.api.routers.admin.get_feedback_by_id", return_value=row), \
             patch("cqc_lem.utilities.feedback.issue_service.file_feedback_issue") as filer, \
             patch("cqc_lem.api.routers.admin.record_feedback_review") as recorder:
            r = client.post("/api/admin/feedback/9/review", json={
                "session_token": "tok", "action": "approve",
            })
        assert r.status_code == 409
        filer.assert_not_called()
        recorder.assert_not_called()

    def test_already_clustered_row_cannot_be_dismissed(self, client):
        row = {"id": 10, "status": "clustered", "github_issue_number": None}
        with _auth(_ADMIN_USER)["get_session"], _auth(_ADMIN_USER)["is_admin"], \
             patch("cqc_lem.api.routers.admin.get_feedback_by_id", return_value=row), \
             patch("cqc_lem.api.routers.admin.record_feedback_review") as recorder:
            r = client.post("/api/admin/feedback/10/review", json={
                "session_token": "tok", "action": "dismiss",
            })
        assert r.status_code == 409
        recorder.assert_not_called()

    def test_new_row_is_still_reviewable(self, client):
        row = {"id": 11, "status": "new", "github_issue_number": None}
        with _auth(_ADMIN_USER)["get_session"], _auth(_ADMIN_USER)["is_admin"], \
             patch("cqc_lem.api.routers.admin.get_feedback_by_id", return_value=row), \
             patch("cqc_lem.api.routers.admin.record_feedback_review", return_value=True):
            r = client.post("/api/admin/feedback/11/review", json={
                "session_token": "tok", "action": "dismiss",
            })
        assert r.status_code == 200

    def test_404_when_feedback_row_missing(self, client):
        with _auth(_ADMIN_USER)["get_session"], _auth(_ADMIN_USER)["is_admin"], \
             patch("cqc_lem.api.routers.admin.get_feedback_by_id", return_value=None):
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

    def test_approve_persists_issue_created_and_list_reflects_it(self, client):
        """Issue #1070: the issue-filed transition must persist the triage status.

        The feedback list endpoint must return it, and the SPA derives its buttons from that
        status. This test exercises the real end-to-end path through `file_feedback_issue` rather
        than mocking it, using a shared in-memory DB so the review write and the list read see the
        same row.
        """
        store = {
            5: {
                "id": 5, "user_id": 1, "source": "widget", "type_hint": "bug",
                "body": "the approve button does nothing", "context_json": None,
                "embedding": None, "cluster_id": None, "github_issue_number": None,
                "status": "new", "sentiment": None, "reviewed_by": None,
                "reviewed_at": None, "created_at": None,
                "email": "user@x.com", "is_admin": 0,
            },
        }

        class FakeCur:
            def __init__(self, dictionary):
                self.dictionary = dictionary
                self._rows = []

            def execute(self, sql, params):
                if "FROM feedback WHERE id=%s" in sql:
                    self._rows = [store.get(params[0])]
                elif "FROM feedback f LEFT JOIN users u" in sql:
                    self._rows = []
                    for row in store.values():
                        item = dict(row)
                        item["email"] = item.get("email")
                        item["is_admin"] = item.get("is_admin")
                        self._rows.append(item)
                elif sql.startswith("UPDATE feedback SET"):
                    set_part = sql[len("UPDATE feedback SET "):].split(" WHERE ")[0]
                    cols = [c.split("=")[0].strip() for c in set_part.split(",")]
                    feedback_id = params[-1]
                    row = store[feedback_id]
                    for col, val in zip(cols, params[:-1]):
                        row[col] = val
                    self._rows = []
                else:
                    self._rows = []

            def fetchone(self):
                return self._rows[0] if self._rows else None

            def fetchall(self):
                return self._rows

            @property
            def rowcount(self):
                return 1

            def close(self):
                pass

        class FakeConn:
            def cursor(self, dictionary=True):
                return FakeCur(dictionary)

            def close(self):
                pass

            def commit(self):
                pass

        with _auth(_ADMIN_USER)["get_session"], _auth(_ADMIN_USER)["is_admin"], \
             patch("cqc_lem.utilities.feedback.issue_service.classify_feedback",
                   return_value=_classification()), \
             patch("cqc_lem.utilities.feedback.issue_service.create_github_issue",
                   return_value=1068), \
             patch("cqc_lem.utilities.feedback.issue_service.comment_on_issue",
                   return_value=True), \
             patch("cqc_lem.utilities.feedback.issue_service.embed_text",
                   return_value=None), \
             patch("cqc_lem.utilities.feedback.issue_service.count_feedback_filed_by_user",
                   return_value=0), \
             patch("cqc_lem.utilities.feedback.issue_service.get_open_feedback_clusters",
                   return_value=[]), \
             patch("cqc_lem.platform.db.connection.get_db_connection", return_value=FakeConn()), \
             patch("cqc_lem.utilities.db.datetime") as dt_mock:
            dt_mock.now.return_value = datetime(2026, 8, 7, 23, 0, 0, tzinfo=timezone.utc)

            r1 = client.post("/api/admin/feedback/5/review", json={
                "session_token": "tok", "action": "approve",
            })
            assert r1.status_code == 200
            assert r1.json()["detail"]["filed"] is True

            r2 = client.get("/api/admin/feedback", params={"session_token": "tok"})
            assert r2.status_code == 200
            item = r2.json()["detail"]["items"][0]
            assert item["status"] == "issue_created"
            assert item["github_issue_number"] == 1068
            assert item["reviewed_by"] == 7


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
             patch("cqc_lem.api.routers.auth.get_user_email", return_value="admin@x.com"), \
             patch("cqc_lem.api.routers.auth.get_user_analytics_profile", return_value=profile), \
             patch("cqc_lem.api.routers.auth.is_user_admin", return_value=True):
            r = client.get("/api/auth/session", params={"session_token": "tok"})
        assert r.status_code == 200
        assert r.json()["detail"]["is_admin"] is True
