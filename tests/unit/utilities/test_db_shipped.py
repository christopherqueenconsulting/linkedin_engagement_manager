"""Unit tests for the shipped-notice DB helpers (issue #502)."""

from datetime import datetime

import mysql.connector
import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _conn(rowcount=1, fetchone=None, fetchall=None, lastrowid=None):
    conn = MagicMock()
    cur = MagicMock()
    cur.rowcount = rowcount
    cur.lastrowid = lastrowid
    cur.fetchone.return_value = fetchone
    cur.fetchall.return_value = fetchall or []
    conn.cursor.return_value = cur
    return conn, cur


def _erroring_conn():
    conn = MagicMock()
    cur = MagicMock()
    cur.execute.side_effect = mysql.connector.Error("boom")
    conn.cursor.return_value = cur
    return conn, cur


class TestReporterMapping:
    def test_maps_feedback_to_issue_and_cluster(self):
        conn, cur = _conn(fetchall=[(4,), (7,)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_feedback_reporters_for_issue
            assert get_feedback_reporters_for_issue(498) == [4, 7]
        sql, params = cur.execute.call_args[0]
        # Both the directly-stamped rows AND rows that only carry the seed's cluster_id.
        assert "f.github_issue_number = %s OR s.github_issue_number = %s" in sql
        assert "f.user_id IS NOT NULL" in sql
        assert params == (498, 498)

    def test_no_issue_number_and_db_errors_return_nothing(self):
        from cqc_lem.utilities.db import get_feedback_reporters_for_issue
        assert get_feedback_reporters_for_issue(None) == []
        conn, _cur = _erroring_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            assert get_feedback_reporters_for_issue(498) == []


class TestResolveCluster:
    def test_moves_open_rows_to_resolved_and_leaves_dismissed_alone(self):
        conn, cur = _conn(rowcount=3)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import mark_feedback_resolved_for_issue
            assert mark_feedback_resolved_for_issue(498) == 3
        sql, params = cur.execute.call_args[0]
        assert "f.status NOT IN" in sql
        assert params == ("resolved", 498, 498, "resolved", "dismissed")

    def test_resolves_cluster_attached_rows_the_same_way_reporters_are_mapped(self):
        """The UPDATE must span the same self-join as get_feedback_reporters_for_issue, or a row
        attached by cluster_id before the issue number propagated stays open forever."""
        conn, cur = _conn(rowcount=2)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import mark_feedback_resolved_for_issue
            mark_feedback_resolved_for_issue(498)
        sql = cur.execute.call_args[0][0]
        assert "LEFT JOIN feedback s ON s.id = f.cluster_id" in sql
        assert "f.github_issue_number = %s OR s.github_issue_number = %s" in sql

    def test_zero_on_no_issue_or_error(self):
        from cqc_lem.utilities.db import mark_feedback_resolved_for_issue
        assert mark_feedback_resolved_for_issue(0) == 0
        conn, _cur = _erroring_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            assert mark_feedback_resolved_for_issue(498) == 0


class TestRecordShippedNotice:
    def test_returns_the_new_notice_id(self):
        conn, cur = _conn(lastrowid=11)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_shipped_notice
            assert record_shipped_notice(498, "**Fixed:** X (#498)", pr_number=539,
                                         title="fix: x") == 11
        sql, params = cur.execute.call_args[0]
        assert "INSERT IGNORE INTO shipped_notices" in sql
        assert params == (498, 539, "fix: x", "**Fixed:** X (#498)")

    def test_reuses_the_existing_row_when_the_insert_was_ignored(self):
        conn, cur = _conn(lastrowid=0, fetchone=(11,))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_shipped_notice
            assert record_shipped_notice(498, "**Fixed:** X (#498)") == 11
        assert "SELECT id FROM shipped_notices" in cur.execute.call_args[0][0]

    def test_rejects_incomplete_input_and_survives_errors(self):
        from cqc_lem.utilities.db import record_shipped_notice
        assert record_shipped_notice(0, "line") is None
        assert record_shipped_notice(498, "   ") is None
        conn, _cur = _erroring_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            assert record_shipped_notice(498, "line") is None


class TestRecipients:
    def test_first_insert_wins_and_repeats_return_false(self):
        from cqc_lem.utilities.db import record_shipped_notice_recipient
        conn, cur = _conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            assert record_shipped_notice_recipient(11, 4) is True
        assert "INSERT IGNORE INTO shipped_notice_recipients" in cur.execute.call_args[0][0]

        conn, _cur = _conn(rowcount=0)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            assert record_shipped_notice_recipient(11, 4) is False

    def test_reads_back_who_was_already_told(self):
        from cqc_lem.utilities.db import get_shipped_notice_recipient_ids
        conn, cur = _conn(fetchall=[(4,), (7,)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            assert get_shipped_notice_recipient_ids(11) == [4, 7]
        assert cur.execute.call_args[0][1] == (11,)

        assert get_shipped_notice_recipient_ids(None) == []
        conn, _cur = _erroring_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            assert get_shipped_notice_recipient_ids(11) == []

    def test_missing_ids_and_errors_are_false(self):
        from cqc_lem.utilities.db import record_shipped_notice_recipient
        assert record_shipped_notice_recipient(None, 4) is False
        assert record_shipped_notice_recipient(11, None) is False
        conn, _cur = _erroring_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            assert record_shipped_notice_recipient(11, 4) is False


class TestUnseenNotices:
    def test_delay_hours_is_what_schedules_the_csat(self):
        row = {"id": 11, "github_issue_number": 498, "changelog_line": "**Fixed:** X (#498)",
               "shipped_at": datetime(2026, 7, 25, 10, 0)}
        conn, cur = _conn(fetchall=[row])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_unseen_shipped_notices
            assert get_unseen_shipped_notices(4, delay_hours=24, limit=5) == [row]
        sql, params = cur.execute.call_args[0]
        assert "r.seen_at IS NULL" in sql
        assert "INTERVAL %s HOUR" in sql
        assert params == (4, 24, 5)

    def test_negative_delay_is_clamped_and_errors_return_empty(self):
        conn, cur = _conn(fetchall=[])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_unseen_shipped_notices
            get_unseen_shipped_notices(4, delay_hours=-5)
        assert cur.execute.call_args[0][1][1] == 0

        conn, _cur = _erroring_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_unseen_shipped_notices
            assert get_unseen_shipped_notices(4) == []


class TestSeenAndChangelog:
    def test_only_the_first_acknowledgement_writes(self):
        from cqc_lem.utilities.db import mark_shipped_notice_seen
        conn, cur = _conn(rowcount=1)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            assert mark_shipped_notice_seen(11, 4) is True
        assert "seen_at IS NULL" in cur.execute.call_args[0][0]

        conn, _cur = _conn(rowcount=0)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            assert mark_shipped_notice_seen(11, 4) is False

        conn, _cur = _erroring_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            assert mark_shipped_notice_seen(11, 4) is False

    def test_changelog_is_newest_first(self):
        rows = [{"id": 12, "changelog_line": "b"}, {"id": 11, "changelog_line": "a"}]
        conn, cur = _conn(fetchall=rows)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_shipped_notices
            assert get_recent_shipped_notices(2) == rows
        sql, params = cur.execute.call_args[0]
        assert "ORDER BY shipped_at DESC" in sql
        assert params == (2,)

        conn, _cur = _erroring_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_recent_shipped_notices
            assert get_recent_shipped_notices() == []

    def test_notice_lookup_by_issue(self):
        row = {"id": 11, "github_issue_number": 498}
        conn, cur = _conn(fetchone=row)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_shipped_notice_by_issue
            assert get_shipped_notice_by_issue(498) == row
        assert cur.execute.call_args[0][1] == (498,)

        conn, _cur = _erroring_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_shipped_notice_by_issue
            assert get_shipped_notice_by_issue(498) is None
