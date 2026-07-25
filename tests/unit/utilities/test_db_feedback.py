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


def _dict_conn(rows=None, one=None, rowcount=1):
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = one
    cur.rowcount = rowcount
    conn.cursor.return_value = cur
    return conn, cur


class TestGetFeedbackById:
    def test_returns_the_row(self):
        conn, cur = _dict_conn(one={"id": 5, "body": "broken"})
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_feedback_by_id
            assert get_feedback_by_id(5) == {"id": 5, "body": "broken"}
        sql, params = cur.execute.call_args[0]
        assert "FROM feedback WHERE id=%s" in sql
        assert params == (5,)

    def test_db_error_returns_none(self):
        import mysql.connector
        conn, cur = _dict_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn), patch(f"{_DB}.log_error"):
            from cqc_lem.utilities.db import get_feedback_by_id
            assert get_feedback_by_id(5) is None
        conn.close.assert_called_once()


class TestGetUnprocessedFeedback:
    def test_defaults_to_new_only_and_orders_fifo(self):
        conn, cur = _dict_conn(rows=[{"id": 1}])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_unprocessed_feedback
            assert get_unprocessed_feedback(limit=5) == [{"id": 1}]
        sql, params = cur.execute.call_args[0]
        assert "status IN (%s)" in sql
        assert "cluster_id IS NULL" in sql
        assert "ORDER BY created_at ASC, id ASC" in sql
        assert params == ("new", 5)

    def test_widened_statuses_are_all_bound_as_parameters(self):
        conn, cur = _dict_conn(rows=[])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_unprocessed_feedback, FeedbackStatus
            get_unprocessed_feedback(limit=9, statuses=(FeedbackStatus.NEW,
                                                        FeedbackStatus.TRIAGED))
        sql, params = cur.execute.call_args[0]
        assert "status IN (%s,%s)" in sql
        assert params == ("new", "triaged", 9)

    def test_unknown_status_values_are_rejected_without_touching_db(self):
        with patch(f"{_DB}.get_db_connection") as get_conn:
            from cqc_lem.utilities.db import get_unprocessed_feedback
            assert get_unprocessed_feedback(statuses=("'; DROP TABLE feedback; --",)) == []
            assert get_unprocessed_feedback(statuses=()) == []
        get_conn.assert_not_called()

    def test_db_error_returns_empty_list(self):
        import mysql.connector
        conn, cur = _dict_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn), patch(f"{_DB}.log_error"):
            from cqc_lem.utilities.db import get_unprocessed_feedback
            assert get_unprocessed_feedback() == []


class TestGetOpenFeedbackClusters:
    def test_groups_on_the_seed_row_and_counts_distinct_reporters(self):
        rows = [{"cluster_id": 7, "body": "x", "github_issue_number": 101, "item_count": 3,
                 "reporter_count": 2}]
        conn, cur = _dict_conn(rows=rows)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_open_feedback_clusters
            assert get_open_feedback_clusters(limit=50) == rows
        sql, params = cur.execute.call_args[0]
        assert "s.cluster_id = s.id" in sql                   # only seeds
        assert "COUNT(DISTINCT m.user_id) AS reporter_count" in sql
        assert "s.status IN ('clustered','issue_created')" in sql
        assert params == (50,)

    def test_db_error_returns_empty_list(self):
        import mysql.connector
        conn, cur = _dict_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn), patch(f"{_DB}.log_error"):
            from cqc_lem.utilities.db import get_open_feedback_clusters
            assert get_open_feedback_clusters() == []


class TestCountFeedbackFiledByUser:
    def test_counts_only_rows_that_reached_github_in_the_window(self):
        conn, cur = _dict_conn(one=(4,))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_feedback_filed_by_user
            assert count_feedback_filed_by_user(9, hours=12) == 4
        sql, params = cur.execute.call_args[0]
        assert "github_issue_number IS NOT NULL" in sql
        assert "INTERVAL %s HOUR" in sql
        assert params == (9, 12)

    def test_anonymous_feedback_is_never_counted(self):
        with patch(f"{_DB}.get_db_connection") as get_conn:
            from cqc_lem.utilities.db import count_feedback_filed_by_user
            assert count_feedback_filed_by_user(None) == 0
        get_conn.assert_not_called()

    def test_null_count_and_db_error_are_zero(self):
        import mysql.connector
        conn, cur = _dict_conn(one=(None,))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_feedback_filed_by_user
            assert count_feedback_filed_by_user(9) == 0
        conn, cur = _dict_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn), patch(f"{_DB}.log_error"):
            from cqc_lem.utilities.db import count_feedback_filed_by_user
            assert count_feedback_filed_by_user(9) == 0


class TestUpdateFeedbackTriage:
    def test_writes_only_the_fields_passed(self):
        conn, cur = _dict_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_feedback_triage, FeedbackStatus
            assert update_feedback_triage(3, status=FeedbackStatus.ISSUE_CREATED,
                                         cluster_id=3, github_issue_number=88,
                                         embedding=[0.5, 0.25]) is True
        sql, params = cur.execute.call_args[0]
        assert sql == ("UPDATE feedback SET status=%s, cluster_id=%s, github_issue_number=%s, "
                       "embedding=%s WHERE id=%s")
        assert params == ("issue_created", 3, 88, json.dumps([0.5, 0.25]), 3)
        conn.commit.assert_called_once()

    def test_embedding_only_update_leaves_status_alone(self):
        conn, cur = _dict_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_feedback_triage
            update_feedback_triage(3, embedding=[1.0])
        sql, params = cur.execute.call_args[0]
        assert sql == "UPDATE feedback SET embedding=%s WHERE id=%s"
        assert params == (json.dumps([1.0]), 3)

    def test_no_fields_is_a_no_op_that_never_opens_a_connection(self):
        with patch(f"{_DB}.get_db_connection") as get_conn:
            from cqc_lem.utilities.db import update_feedback_triage
            assert update_feedback_triage(3) is False
        get_conn.assert_not_called()

    def test_missing_row_returns_false(self):
        conn, cur = _dict_conn(rowcount=0)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_feedback_triage, FeedbackStatus
            assert update_feedback_triage(99, status=FeedbackStatus.TRIAGED) is False

    def test_over_long_sentiment_is_truncated(self):
        conn, cur = _dict_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_feedback_triage
            update_feedback_triage(3, sentiment="s" * 40)
        assert len(cur.execute.call_args[0][1][0]) == 16

    def test_db_error_returns_false_and_closes(self):
        import mysql.connector
        conn, cur = _dict_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_DB}.get_db_connection", return_value=conn), patch(f"{_DB}.log_error"):
            from cqc_lem.utilities.db import update_feedback_triage, FeedbackStatus
            assert update_feedback_triage(3, status=FeedbackStatus.TRIAGED) is False
        cur.close.assert_called_once()
        conn.close.assert_called_once()
