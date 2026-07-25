"""Integration test for the in-app feedback capture loop (#496).

Drives the real POST /api/feedback handler through the real db.insert_feedback into a stateful fake
cursor that models the `feedback` table, so a widget submission provably lands as a persisted row —
the acceptance criterion — without needing a live MySQL container.
"""
import json

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.integration

_DB = "cqc_lem.utilities.db"


class _FakeCursor:
    """Minimal MySQL-ish cursor over an in-memory `feedback` store."""

    def __init__(self, rows):
        self.rows = rows
        self._result = []
        self.rowcount = 0
        self.lastrowid = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO feedback"):
            user_id, source, type_hint, body, context_json, sentiment = params
            self.lastrowid = len(self.rows) + 1
            self.rows.append({
                "id": self.lastrowid, "user_id": user_id, "source": source,
                "type_hint": type_hint, "body": body, "context_json": context_json,
                "sentiment": sentiment, "status": "new",
            })
            self.rowcount = 1
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def close(self):
        pass


class _FakeConn:
    def __init__(self, rows):
        self._cur = _FakeCursor(rows)

    def cursor(self, *a, **k):
        return self._cur

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from cqc_lem.api.main import app
    return TestClient(app, raise_server_exceptions=False)


class TestFeedbackCapture:
    def test_widget_submission_is_persisted_with_its_context(self, client):
        rows: list = []

        with patch(f"{_DB}.get_db_connection", side_effect=lambda *a, **k: _FakeConn(rows)), \
             patch("cqc_lem.api.main.get_session_user_id", return_value=4):
            response = client.post("/api/feedback", json={
                "session_token": "sess-abc",
                "body": "The Content tab throws a 500 when I approve a carousel",
                "type_hint": "bug",
                "screenshot": "data:image/png;base64,AAAA",
                "context": {"route": "/content?tab=review", "app_version": "0.77.0",
                            "posthog_session_id": "ph-session-1"},
            })

        assert response.status_code == 200
        feedback_id = response.json()["detail"]["feedback_id"]

        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == feedback_id
        assert row["user_id"] == 4
        assert row["source"] == "widget"
        assert row["type_hint"] == "bug"
        assert row["body"].startswith("The Content tab throws a 500")
        context = json.loads(row["context_json"])
        assert context["route"] == "/content?tab=review"
        assert context["app_version"] == "0.77.0"
        assert context["posthog_session_id"] == "ph-session-1"
        assert context["screenshot"] == "data:image/png;base64,AAAA"

    def test_anonymous_submission_persists_with_null_user(self, client):
        rows: list = []

        with patch(f"{_DB}.get_db_connection", side_effect=lambda *a, **k: _FakeConn(rows)):
            response = client.post("/api/feedback", json={"body": "Love the new dashboard",
                                                          "type_hint": "praise"})

        assert response.status_code == 200
        assert len(rows) == 1
        assert rows[0]["user_id"] is None
        assert rows[0]["context_json"] is None
