"""Unit tests for the feedback DB helper (issue #496)."""

import json

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _conn(lastrowid=7):
    conn = MagicMock()
    cur = MagicMock()
    cur.lastrowid = lastrowid
    conn.cursor.return_value = cur
    return conn, cur


class TestInsertFeedback:
    def test_insert_returns_id_and_serializes_context(self):
        conn, cur = _conn(lastrowid=42)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import insert_feedback, FeedbackSource
            got = insert_feedback("The schedule tab 500s", user_id=3,
                                  source=FeedbackSource.WIDGET, type_hint="bug",
                                  context={"route": "/content"}, sentiment="negative")
        assert got == 42
        sql, params = cur.execute.call_args[0]
        assert "INSERT INTO feedback" in sql
        assert params[0] == 3
        assert params[1] == "widget"
        assert params[2] == "bug"
        assert params[3] == "The schedule tab 500s"
        assert json.loads(params[4]) == {"route": "/content"}
        assert params[5] == "negative"
        conn.commit.assert_called_once()

    def test_anonymous_and_empty_context_store_null(self):
        conn, cur = _conn(lastrowid=8)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import insert_feedback
            got = insert_feedback("nice work")
        assert got == 8
        params = cur.execute.call_args[0][1]
        assert params[0] is None       # anonymous — NULL user_id
        assert params[1] == "widget"   # default source
        assert params[2] is None
        assert params[4] is None       # no context -> NULL
        assert params[5] is None

    def test_blank_body_is_rejected_without_touching_db(self):
        with patch(f"{_DB}.get_db_connection") as get_conn:
            from cqc_lem.utilities.db import insert_feedback
            assert insert_feedback("   ") is None
            assert insert_feedback(None) is None
        get_conn.assert_not_called()

    def test_over_long_type_hint_and_sentiment_are_truncated(self):
        conn, cur = _conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import insert_feedback
            insert_feedback("body", type_hint="b" * 80, sentiment="s" * 40)
        params = cur.execute.call_args[0][1]
        assert len(params[2]) == 32  # feedback.type_hint VARCHAR(32)
        assert len(params[5]) == 16  # feedback.sentiment VARCHAR(16)

    def test_db_error_returns_none_and_closes(self):
        import mysql.connector
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn), \
                patch(f"{_DB}.log_error") as logged:
            from cqc_lem.utilities.db import insert_feedback
            assert insert_feedback("body", user_id=1) is None
        cur.close.assert_called_once()
        conn.close.assert_called_once()
        assert logged.call_args.kwargs["user_id"] == 1
        assert isinstance(logged.call_args.kwargs["exc"], mysql.connector.Error)


class TestFeedbackEnums:
    def test_source_values_match_the_migration_enum(self):
        from cqc_lem.utilities.db import FeedbackSource
        assert {str(s) for s in FeedbackSource} == {"widget", "bug", "nps", "review", "passive"}

    def test_status_values_match_the_migration_enum(self):
        from cqc_lem.utilities.db import FeedbackStatus
        assert {str(s) for s in FeedbackStatus} == {
            "new", "triaged", "clustered", "issue_created", "resolved", "dismissed"}
