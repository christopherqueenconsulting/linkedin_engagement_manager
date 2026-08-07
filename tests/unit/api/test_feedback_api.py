"""Unit tests for the in-app feedback endpoint (issue #496)."""

from unittest.mock import patch

import pytest

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


class TestSubmitFeedback:
    def test_persists_with_session_user_and_context(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=9), \
             patch("cqc_lem.api.main.insert_feedback", return_value=101) as ins:
            resp = client.post("/api/feedback", json={
                "session_token": "tok", "body": "  Scheduling a post 500s  ",
                "type_hint": "bug",
                "context": {"route": "/content", "app_version": "0.77.0",
                            "posthog_session_id": "ph-1"}})
        assert resp.status_code == 200
        assert resp.json()["detail"]["feedback_id"] == 101
        from cqc_lem.utilities.db import FeedbackSource
        assert ins.call_args[0][0] == "Scheduling a post 500s"  # trimmed
        assert ins.call_args.kwargs["user_id"] == 9
        assert ins.call_args.kwargs["source"] == FeedbackSource.WIDGET
        assert ins.call_args.kwargs["type_hint"] == "bug"
        assert ins.call_args.kwargs["context"]["route"] == "/content"
        assert ins.call_args.kwargs["context"]["posthog_session_id"] == "ph-1"

    def test_anonymous_submission_has_no_user_id(self, client):
        with patch("cqc_lem.api.main.get_session_user_id") as sess, \
             patch("cqc_lem.api.main.insert_feedback", return_value=102) as ins:
            resp = client.post("/api/feedback", json={"body": "love the new tabs"})
        assert resp.status_code == 200
        sess.assert_not_called()  # no token -> never hit the session table
        assert ins.call_args.kwargs["user_id"] is None

    def test_invalid_session_token_falls_back_to_anonymous(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None), \
             patch("cqc_lem.api.main.insert_feedback", return_value=103) as ins:
            resp = client.post("/api/feedback", json={"session_token": "stale", "body": "hi"})
        assert resp.status_code == 200
        assert ins.call_args.kwargs["user_id"] is None

    def test_screenshot_rides_along_in_context(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.main.insert_feedback", return_value=104) as ins:
            resp = client.post("/api/feedback", json={
                "session_token": "tok", "body": "see attached",
                "screenshot": "data:image/png;base64,AAAA"})
        assert resp.status_code == 200
        assert ins.call_args.kwargs["context"]["screenshot"] == "data:image/png;base64,AAAA"

    def test_oversized_context_is_dropped_but_screenshot_kept(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.main.insert_feedback", return_value=105) as ins:
            resp = client.post("/api/feedback", json={
                "session_token": "tok", "body": "big", "screenshot": "data:image/png;base64,BBBB",
                "context": {"junk": "x" * 9000}})
        assert resp.status_code == 200
        ctx = ins.call_args.kwargs["context"]
        assert ctx == {"truncated": True, "screenshot": "data:image/png;base64,BBBB"}

    def test_blank_body_rejected_422(self, client):
        with patch("cqc_lem.api.main.insert_feedback") as ins:
            resp = client.post("/api/feedback", json={"body": "   "})
        assert resp.status_code == 422
        ins.assert_not_called()

    def test_over_long_body_rejected_422(self, client):
        with patch("cqc_lem.api.main.insert_feedback") as ins:
            resp = client.post("/api/feedback", json={"body": "x" * 5001})
        assert resp.status_code == 422
        ins.assert_not_called()

    def test_unknown_source_rejected_422(self, client):
        with patch("cqc_lem.api.main.insert_feedback") as ins:
            resp = client.post("/api/feedback", json={"body": "hi", "source": "carrier-pigeon"})
        assert resp.status_code == 422
        ins.assert_not_called()

    def test_alternate_source_accepted(self, client):
        with patch("cqc_lem.api.main.insert_feedback", return_value=106) as ins:
            resp = client.post("/api/feedback", json={"body": "9/10", "source": "nps"})
        assert resp.status_code == 200
        from cqc_lem.utilities.db import FeedbackSource
        assert ins.call_args.kwargs["source"] == FeedbackSource.NPS

    def test_db_failure_returns_500(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=1), \
             patch("cqc_lem.api.main.insert_feedback", return_value=None):
            resp = client.post("/api/feedback", json={"session_token": "tok", "body": "hi"})
        assert resp.status_code == 500


class TestFeedbackMigration:
    def test_migration_columns_cover_the_issue_spec(self):
        import glob
        import os
        root = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                            "compose", "local", "database", "migrations")
        files = glob.glob(os.path.join(root, "V*__add_feedback.sql"))
        assert len(files) == 1, "expected exactly one add_feedback migration"
        version = os.path.basename(files[0]).split("__")[0][1:]
        assert len(version) == 14 and version.isdigit(), "new migrations must use a timestamp version"
        with open(files[0]) as f:
            sql = f.read()
        for col in ("user_id", "source", "type_hint", "body", "context_json", "embedding",
                    "cluster_id", "github_issue_number", "status", "sentiment", "created_at"):
            assert col in sql, f"missing column {col}"

    def test_status_enum_matches_the_feedback_status_vocabulary(self):
        # issue #668: a FeedbackStatus member with no matching ENUM value only fails in PRODUCTION,
        # as "1265 Data truncated for column 'status'" — never in a unit test. Catch it at build time.
        # The vocabulary is the LAST declaration in version order, so widening the ENUM the only
        # legal way — a new ALTER migration — satisfies this test. An already-merged migration must
        # never be edited to appease it: Flyway validates applied ones by version+checksum.
        import glob
        import os
        import re

        from cqc_lem.utilities.db import FeedbackStatus
        root = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                            "compose", "local", "database", "migrations")
        declared = None
        for path in sorted(glob.glob(os.path.join(root, "V*.sql"))):
            with open(path) as f:
                sql = f.read()
            for statement in sql.split(";"):
                if not re.search(r"TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?feedback`?\b",
                                 statement, re.IGNORECASE):
                    continue
                match = re.search(r"\bstatus\s+ENUM\(([^)]*)\)", statement, re.IGNORECASE)
                if match:
                    declared = set(re.findall(r"'([^']*)'", match.group(1)))
        assert declared, "no migration declares feedback.status as an ENUM"
        assert {s.value for s in FeedbackStatus} == declared
