"""Unit tests for the feedback DB helper (issue #496)."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"
_GET_CONN = "cqc_lem.platform.db.connection.get_db_connection"
_FEEDBACK = "cqc_lem.platform.db.repositories.feedback"


def _conn(lastrowid=7):
    conn = MagicMock()
    cur = MagicMock()
    cur.lastrowid = lastrowid
    conn.cursor.return_value = cur
    return conn, cur


class TestInsertFeedback:
    def test_insert_returns_id_and_serializes_context(self):
        conn, cur = _conn(lastrowid=42)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import FeedbackSource, insert_feedback
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
        with patch(f"{_GET_CONN}", return_value=conn):
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
        with patch(f"{_GET_CONN}") as get_conn:
            from cqc_lem.utilities.db import insert_feedback
            assert insert_feedback("   ") is None
            assert insert_feedback(None) is None
        get_conn.assert_not_called()

    def test_over_long_type_hint_and_sentiment_are_truncated(self):
        conn, cur = _conn()
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import insert_feedback
            insert_feedback("body", type_hint="b" * 80, sentiment="s" * 40)
        params = cur.execute.call_args[0][1]
        assert len(params[2]) == 32  # feedback.type_hint VARCHAR(32)
        assert len(params[5]) == 16  # feedback.sentiment VARCHAR(16)

    def test_db_error_returns_none_and_closes(self):
        import mysql.connector
        conn, cur = _conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_GET_CONN}", return_value=conn), \
                patch(f"{_FEEDBACK}.log_error") as logged:
            from cqc_lem.utilities.db import insert_feedback
            assert insert_feedback("body", user_id=1) is None
        cur.close.assert_called_once()
        conn.close.assert_called_once()
        assert logged.call_args.kwargs["user_id"] == 1
        assert isinstance(logged.call_args.kwargs["exc"], mysql.connector.Error)


class TestFeedbackEnums:
    def test_source_values_match_the_migration_enum(self):
        from cqc_lem.utilities.db import FeedbackSource
        # 'csat' was appended by V20260725104900 for the post-fix micro-CSAT (issue #502).
        assert {str(s) for s in FeedbackSource} == {"widget", "bug", "nps", "review", "passive",
                                                    "csat"}

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
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_feedback_by_id
            assert get_feedback_by_id(5) == {"id": 5, "body": "broken"}
        sql, params = cur.execute.call_args[0]
        assert "FROM feedback WHERE id=%s" in sql
        assert params == (5,)

    def test_db_error_returns_none(self):
        import mysql.connector
        conn, cur = _dict_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_FEEDBACK}.log_error"):
            from cqc_lem.utilities.db import get_feedback_by_id
            assert get_feedback_by_id(5) is None
        conn.close.assert_called_once()


class TestGetUnprocessedFeedback:
    def test_defaults_to_new_only_and_orders_fifo(self):
        conn, cur = _dict_conn(rows=[{"id": 1}])
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_unprocessed_feedback
            assert get_unprocessed_feedback(limit=5) == [{"id": 1}]
        sql, params = cur.execute.call_args[0]
        assert "f.status IN (%s)" in sql
        assert "f.cluster_id IS NULL" in sql
        assert "ORDER BY f.created_at ASC, f.id ASC" in sql
        assert "JOIN users" not in sql                    # no admin filter unless asked for
        assert params == ("new", 5)

    def test_widened_statuses_are_all_bound_as_parameters(self):
        conn, cur = _dict_conn(rows=[])
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import FeedbackStatus, get_unprocessed_feedback
            get_unprocessed_feedback(limit=9, statuses=(FeedbackStatus.NEW,
                                                        FeedbackStatus.TRIAGED))
        sql, params = cur.execute.call_args[0]
        assert "f.status IN (%s,%s)" in sql
        assert params == ("new", "triaged", 9)

    def test_admin_only_filters_in_sql_not_in_the_caller(self, monkeypatch):
        """Issue #793: parked non-admin rows stay `new` with a NULL cluster forever, so if the
        filter lived in the caller's loop they would fill `limit` every pass and starve admin
        feedback out of the queue permanently.
        """
        monkeypatch.setattr("cqc_lem.utilities.env_constants.ADMIN_USER_EMAILS", "")
        conn, cur = _dict_conn(rows=[])
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_unprocessed_feedback
            get_unprocessed_feedback(limit=7, admin_only=True)
        sql, params = cur.execute.call_args[0]
        assert "LEFT JOIN users au ON au.id = f.user_id AND (au.is_admin = 1)" in sql
        assert "AND au.id IS NOT NULL" in sql
        assert params == ("new", 7)

    def test_admin_only_honours_the_email_allowlist(self, monkeypatch):
        monkeypatch.setattr("cqc_lem.utilities.env_constants.ADMIN_USER_EMAILS",
                            " Owner@Example.com , second@example.com ")
        conn, cur = _dict_conn(rows=[])
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_unprocessed_feedback
            get_unprocessed_feedback(limit=3, admin_only=True)
        sql, params = cur.execute.call_args[0]
        assert "LOWER(au.email) IN (%s,%s)" in sql
        assert params == ("owner@example.com", "second@example.com", "new", 3)

    def test_unknown_status_values_are_rejected_without_touching_db(self):
        with patch(f"{_GET_CONN}") as get_conn:
            from cqc_lem.utilities.db import get_unprocessed_feedback
            assert get_unprocessed_feedback(statuses=("'; DROP TABLE feedback; --",)) == []
            assert get_unprocessed_feedback(statuses=()) == []
        get_conn.assert_not_called()

    def test_db_error_returns_empty_list(self):
        import mysql.connector
        conn, cur = _dict_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_FEEDBACK}.log_error"):
            from cqc_lem.utilities.db import get_unprocessed_feedback
            assert get_unprocessed_feedback() == []


class TestGetOpenFeedbackClusters:
    def test_groups_on_the_seed_row_and_counts_distinct_reporters(self):
        rows = [{"cluster_id": 7, "body": "x", "github_issue_number": 101, "item_count": 3,
                 "reporter_count": 2}]
        conn, cur = _dict_conn(rows=rows)
        with patch(f"{_GET_CONN}", return_value=conn):
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
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_FEEDBACK}.log_error"):
            from cqc_lem.utilities.db import get_open_feedback_clusters
            assert get_open_feedback_clusters() == []


class TestCountFeedbackFiledByUser:
    def test_counts_only_rows_that_reached_github_in_the_window(self):
        conn, cur = _dict_conn(one=(4,))
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import count_feedback_filed_by_user
            assert count_feedback_filed_by_user(9, hours=12) == 4
        sql, params = cur.execute.call_args[0]
        assert "github_issue_number IS NOT NULL" in sql
        assert "INTERVAL %s HOUR" in sql
        assert params == (9, 12)

    def test_anonymous_feedback_is_never_counted(self):
        with patch(f"{_GET_CONN}") as get_conn:
            from cqc_lem.utilities.db import count_feedback_filed_by_user
            assert count_feedback_filed_by_user(None) == 0
        get_conn.assert_not_called()

    def test_null_count_and_db_error_are_zero(self):
        import mysql.connector
        conn, cur = _dict_conn(one=(None,))
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import count_feedback_filed_by_user
            assert count_feedback_filed_by_user(9) == 0
        conn, cur = _dict_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_FEEDBACK}.log_error"):
            from cqc_lem.utilities.db import count_feedback_filed_by_user
            assert count_feedback_filed_by_user(9) == 0


class TestUpdateFeedbackTriage:
    def test_writes_only_the_fields_passed(self):
        conn, cur = _dict_conn()
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import FeedbackStatus, update_feedback_triage
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
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import update_feedback_triage
            update_feedback_triage(3, embedding=[1.0])
        sql, params = cur.execute.call_args[0]
        assert sql == "UPDATE feedback SET embedding=%s WHERE id=%s"
        assert params == (json.dumps([1.0]), 3)

    def test_no_fields_is_a_no_op_that_never_opens_a_connection(self):
        with patch(f"{_GET_CONN}") as get_conn:
            from cqc_lem.utilities.db import update_feedback_triage
            assert update_feedback_triage(3) is False
        get_conn.assert_not_called()

    def test_missing_row_returns_false(self):
        conn, cur = _dict_conn(rowcount=0)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import FeedbackStatus, update_feedback_triage
            assert update_feedback_triage(99, status=FeedbackStatus.TRIAGED) is False

    def test_over_long_sentiment_is_truncated(self):
        conn, cur = _dict_conn()
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import update_feedback_triage
            update_feedback_triage(3, sentiment="s" * 40)
        assert len(cur.execute.call_args[0][1][0]) == 16

    def test_db_error_returns_false_and_closes(self):
        import mysql.connector
        conn, cur = _dict_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_GET_CONN}", return_value=conn), \
             patch(f"{_FEEDBACK}.log_error") as log:
            from cqc_lem.utilities.db import FeedbackStatus, update_feedback_triage
            assert update_feedback_triage(3, status=FeedbackStatus.TRIAGED) is False
        cur.close.assert_called_once()
        conn.close.assert_called_once()
        # The columns in flight are named, so the next 1265 says which ENUM rejected the value.
        assert "status" in log.call_args[0][0]

    def test_unknown_status_never_reaches_the_enum_column(self):
        # issue #668: `feedback.status` is a MySQL ENUM — an out-of-vocabulary value used to blow up
        # as an opaque "1265 Data truncated for column 'status'" and take the whole UPDATE with it.
        with patch(f"{_GET_CONN}") as get_conn, patch(f"{_FEEDBACK}.log_error") as log:
            from cqc_lem.utilities.db import update_feedback_triage
            assert update_feedback_triage(1, status="pending", cluster_id=4) is False
        get_conn.assert_not_called()
        message = log.call_args[0][0]
        assert "'pending'" in message and "issue_created" in message
        # exc= is what makes it a grouped PostHog $exception like the 1265 it replaces — a refusal
        # logged without one would never reach error tracking or the issue filer.
        assert isinstance(log.call_args.kwargs.get("exc"), ValueError)

    def test_status_spelling_variants_are_accepted(self):
        conn, cur = _dict_conn()
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import update_feedback_triage
            assert update_feedback_triage(3, status=" Issue_Created ") is True
        assert cur.execute.call_args[0][1] == ("issue_created", 3)

    def test_every_feedback_status_member_is_writable(self):
        from cqc_lem.utilities.db import FeedbackStatus, update_feedback_triage
        for member in FeedbackStatus:
            conn, cur = _dict_conn()
            with patch(f"{_GET_CONN}", return_value=conn):
                assert update_feedback_triage(3, status=member) is True
            assert cur.execute.call_args[0][1] == (member.value, 3)


class TestIsUserAdmin:
    def test_true_when_row_has_is_admin(self):
        conn, cur = _dict_conn(one={"is_admin": 1, "email": "someone@example.com"})
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import is_user_admin
            assert is_user_admin(5) is True
        sql, params = cur.execute.call_args[0]
        assert "SELECT is_admin, email FROM users" in sql
        assert params == (5,)

    def test_false_when_row_missing(self):
        conn, cur = _dict_conn(one=None)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import is_user_admin
            assert is_user_admin(5) is False

    def test_false_when_row_is_zero(self, monkeypatch):
        monkeypatch.setattr("cqc_lem.utilities.env_constants.ADMIN_USER_EMAILS", "")
        conn, cur = _dict_conn(one={"is_admin": 0, "email": "someone@example.com"})
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import is_user_admin
            assert is_user_admin(5) is False

    def test_email_allowlist_grants_admin_when_the_column_is_zero(self, monkeypatch):
        """Bootstrap path (#793): with no flagged user the auto-filer parks everything and nobody
        can reach the panel to release it.
        """
        monkeypatch.setattr("cqc_lem.utilities.env_constants.ADMIN_USER_EMAILS",
                            "owner@example.com")
        conn, _ = _dict_conn(one={"is_admin": 0, "email": " Owner@Example.com "})
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import is_user_admin
            assert is_user_admin(5) is True

    def test_empty_allowlist_grants_nobody(self, monkeypatch):
        monkeypatch.setattr("cqc_lem.utilities.env_constants.ADMIN_USER_EMAILS", "  ,  ")
        conn, _ = _dict_conn(one={"is_admin": 0, "email": ""})
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import is_user_admin
            assert is_user_admin(5) is False

    def test_none_user_id_is_false(self):
        with patch(f"{_GET_CONN}") as get_conn:
            from cqc_lem.utilities.db import is_user_admin
            assert is_user_admin(None) is False
        get_conn.assert_not_called()

    def test_db_error_is_false(self):
        import mysql.connector
        conn, cur = _dict_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_DB}.log_error"):
            from cqc_lem.utilities.db import is_user_admin
            assert is_user_admin(5) is False


class TestCountPendingAdminReview:
    def test_counts_the_rows_the_auto_filer_skipped(self, monkeypatch):
        monkeypatch.setattr("cqc_lem.utilities.env_constants.ADMIN_USER_EMAILS", "")
        conn, cur = _dict_conn(one=(4,))
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import count_pending_admin_review
            assert count_pending_admin_review() == 4
        sql, params = cur.execute.call_args[0]
        assert "AND au.id IS NULL" in sql                 # the NON-admin half of the same join
        assert "f.cluster_id IS NULL" in sql
        assert params == ("new",)

    def test_no_row_is_zero(self):
        conn, _ = _dict_conn(one=None)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import count_pending_admin_review
            assert count_pending_admin_review() == 0

    def test_unknown_status_never_touches_the_db(self):
        with patch(f"{_GET_CONN}") as get_conn:
            from cqc_lem.utilities.db import count_pending_admin_review
            assert count_pending_admin_review(statuses=("'; DROP TABLE feedback; --",)) == 0
        get_conn.assert_not_called()

    def test_db_error_is_zero(self):
        import mysql.connector
        conn, cur = _dict_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_DB}.log_error"):
            from cqc_lem.utilities.db import count_pending_admin_review
            assert count_pending_admin_review() == 0


class TestGetFeedbackList:
    def test_returns_rows_joined_with_user_email(self):
        conn, cur = _dict_conn(rows=[{"id": 1, "email": "a@x.com", "is_admin": 1,
                                     "status": "new", "source": "widget"}])
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_feedback_list
            got = get_feedback_list(limit=5, offset=10)
        assert got == [{"id": 1, "email": "a@x.com", "is_admin": 1,
                        "status": "new", "source": "widget"}]
        sql, params = cur.execute.call_args[0]
        assert "FROM feedback f LEFT JOIN users u" in sql
        assert "ORDER BY f.created_at DESC" in sql
        assert "LIMIT %s OFFSET %s" in sql
        assert params == (5, 10)

    def test_status_filter_is_validated_and_bound(self):
        conn, cur = _dict_conn(rows=[])
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import FeedbackSource, FeedbackStatus, get_feedback_list
            get_feedback_list(status=FeedbackStatus.NEW, source=FeedbackSource.WIDGET, limit=2)
        sql, params = cur.execute.call_args[0]
        assert "f.status = %s" in sql
        assert "f.source = %s" in sql
        assert params == ("new", "widget", 2, 0)

    def test_embedding_is_not_dragged_into_the_panel(self):
        """A page of 50 rows would pull 50 full vectors MySQL-side for a column the panel never
        renders.
        """
        conn, cur = _dict_conn(rows=[])
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_feedback_list
            get_feedback_list()
        assert "embedding" not in cur.execute.call_args[0][0]

    def test_allowlisted_reporter_reads_as_admin(self, monkeypatch):
        """The auto-filer's join counts ADMIN_USER_EMAILS as admin, so their reports ARE filed
        automatically — the panel must not show them as still awaiting review.
        """
        monkeypatch.setattr("cqc_lem.utilities.env_constants.ADMIN_USER_EMAILS",
                            "owner@example.com")
        conn, _ = _dict_conn(rows=[{"id": 1, "email": " Owner@Example.com ", "is_admin": 0},
                                   {"id": 2, "email": "someone@example.com", "is_admin": 0},
                                   {"id": 3, "email": None, "is_admin": None}])
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_feedback_list
            got = get_feedback_list()
        assert [r["is_admin"] for r in got] == [1, 0, None]

    def test_invalid_status_returns_empty(self):
        with patch(f"{_GET_CONN}") as get_conn:
            from cqc_lem.utilities.db import get_feedback_list
            assert get_feedback_list(status="not-a-status") == []
        get_conn.assert_not_called()

    def test_db_error_returns_empty_list(self):
        import mysql.connector
        conn, cur = _dict_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_FEEDBACK}.log_error"):
            from cqc_lem.utilities.db import get_feedback_list
            assert get_feedback_list() == []


class TestRecordFeedbackReview:
    def test_records_reviewer_and_status(self):
        conn, cur = _dict_conn()
        with patch(f"{_GET_CONN}", return_value=conn), \
                patch(f"{_FEEDBACK}.datetime") as dt:
            from cqc_lem.utilities.db import FeedbackStatus, record_feedback_review
            now = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)
            dt.now.return_value = now
            assert record_feedback_review(7, 3, status=FeedbackStatus.DISMISSED) is True
        sql, params = cur.execute.call_args[0]
        assert "reviewed_by=%s, reviewed_at=%s, status=%s" in sql
        assert params[:3] == (3, now.replace(tzinfo=None), "dismissed")
        assert params[3] == 7
        conn.commit.assert_called_once()

    def test_can_record_reviewer_without_status(self):
        conn, cur = _dict_conn()
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import record_feedback_review
            assert record_feedback_review(7, 3) is True
        sql, params = cur.execute.call_args[0]
        assert "status" not in sql
        assert params[:1] == (3,)
        assert params[2] == 7

    def test_unknown_status_never_reaches_enum(self):
        with patch(f"{_GET_CONN}") as get_conn, patch(f"{_FEEDBACK}.log_error") as log:
            from cqc_lem.utilities.db import record_feedback_review
            assert record_feedback_review(7, 3, status="pending") is False
        get_conn.assert_not_called()
        assert "pending" in log.call_args[0][0]

    def test_missing_feedback_or_reviewer_is_false(self):
        from cqc_lem.utilities.db import record_feedback_review
        assert record_feedback_review(None, 3) is False
        assert record_feedback_review(7, None) is False

    def test_db_error_returns_false(self):
        import mysql.connector
        conn, cur = _dict_conn()
        cur.execute.side_effect = mysql.connector.Error("boom")
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_FEEDBACK}.log_error"):
            from cqc_lem.utilities.db import record_feedback_review
            assert record_feedback_review(7, 3) is False
