"""Unit tests for DM follow-up queue DB helpers."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestFollowupQueue:
    def test_enqueue_inserts_pending(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            import datetime

            from cqc_lem.utilities.db import enqueue_followup
            ok = enqueue_followup(1, "https://x/in/jane", "Jane", "connection_accepted", 1,
                                  datetime.datetime(2026, 7, 4, 8, 0))
        assert ok is True
        assert "INSERT INTO dm_followups" in cursor.execute.call_args[0][0]
        assert cursor.execute.call_args[0][1][-1] == "pending"

    def test_get_due_returns_rows(self, fake_cursor):
        rows = [{"id": 1, "user_id": 1, "profile_url": "p", "first_name": "Jane",
                 "event_type": "connection_accepted", "next_step": 1}]
        conn, cursor = fake_cursor(fetch_all=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            import datetime

            from cqc_lem.utilities.db import get_due_followups
            out = get_due_followups(datetime.datetime(2026, 7, 4, 9, 0))
        assert out == rows
        assert "due_at <= %s" in cursor.execute.call_args[0][0]
        assert cursor.execute.call_args[0][1] == ("pending", datetime.datetime(2026, 7, 4, 9, 0))

    def test_due_at_reader_and_writers_share_one_clock(self, fake_cursor):
        """Issue #1815 review: `due_at` is NAIVE UTC on both sides of the comparison.

        Every writer of `dm_followups.due_at` and the query that picks the row up go through
        `to_naive_utc` (docs/timezone-contract.md), so an AWARE non-UTC caller is converted rather
        than stored as its local wall clock. Without this, a backoff computed on one clock and
        compared against another lands in the past and the row is due again on the very next
        beat — the bug, with a warning attached.
        """
        import datetime

        # 09:00 in a UTC+2 zone is 07:00 UTC, whichever helper receives it.
        aware = datetime.datetime(2026, 7, 4, 9, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=2)))
        expected = datetime.datetime(2026, 7, 4, 7, 0)

        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import (
                enqueue_followup,
                get_due_followups,
                record_unreadable_read,
            )
            get_due_followups(aware)
            assert cursor.execute.call_args[0][1][1] == expected
            enqueue_followup(1, "p", "Jane", "connection_accepted", 1, aware)
            assert cursor.execute.call_args[0][1][5] == expected
            record_unreadable_read(7, due_at=aware)
            assert cursor.execute.call_args[0][1][0] == expected

    def test_mark_followup(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import mark_followup
            assert mark_followup(5, "sent") is True
        assert cursor.execute.call_args[0][1] == ("sent", 5)
        # Issue #1815 review: mark_followup stays a status setter and nothing else. The
        # unreadable-read reset is its own explicit call, so no future status move can silently
        # wipe a legitimate streak.
        assert "unreadable_reads" not in cursor.execute.call_args[0][0]

    def test_reset_unreadable_reads_clears_the_streak(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import reset_unreadable_reads
            assert reset_unreadable_reads(7) is True
        assert "unreadable_reads = 0" in cursor.execute.call_args[0][0]
        assert cursor.execute.call_args[0][1] == (7,)

    def test_reset_unreadable_reads_tolerates_a_row_that_moved_on(self, fake_cursor):
        # Nothing to reset is not a failure — the row already left the pending ladder.
        conn, _cursor = fake_cursor(rowcount=0)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import reset_unreadable_reads
            assert reset_unreadable_reads(7) is True

    def test_record_unreadable_read_increments_without_moving_due_at(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_unreadable_read
            assert record_unreadable_read(7) is True
        sql = cursor.execute.call_args[0][0]
        assert "unreadable_reads = unreadable_reads + 1" in sql
        assert "due_at" not in sql
        assert cursor.execute.call_args[0][1] == (7, "pending")

    def test_record_unreadable_read_backs_off_due_at(self, fake_cursor):
        import datetime

        conn, cursor = fake_cursor()
        pushed = datetime.datetime(2026, 9, 3, 4, 0)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_unreadable_read
            assert record_unreadable_read(7, due_at=pushed) is True
        sql = cursor.execute.call_args[0][0]
        assert "unreadable_reads = unreadable_reads + 1" in sql
        assert "due_at = %s" in sql
        assert cursor.execute.call_args[0][1] == (pushed, 7, "pending")

    def test_record_unreadable_read_is_false_when_no_pending_row_matched(self, fake_cursor):
        """Issue #1815 review: a zero-match has to be distinguishable from a counted read.

        The caller decides whether to back off from the count it read at the start of the run. If
        the row moved to a terminal status in between, the counter never advanced — reporting
        success would announce a backoff that did not happen, on every beat, forever.
        """
        conn, _cursor = fake_cursor(rowcount=0)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_unreadable_read
            assert record_unreadable_read(7) is False

    def test_stop_followups_returns_count(self, fake_cursor):
        conn, cursor = fake_cursor(rowcount=3)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import stop_followups_for_profile
            assert stop_followups_for_profile(1, "p") == 3
        assert "status='stopped'" in cursor.execute.call_args[0][0]

    def test_most_recent_dm_thread_target(self, fake_cursor):
        """Issue #1770: the sweep's `message_thread` target resolver reads this.

        ANY status row counts — a stopped thread still existed.
        """
        row = {"profile_url": "https://x/in/jane", "first_name": "Jane"}
        conn, cursor = fake_cursor(fetch_one=row)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_most_recent_dm_thread_target
            assert get_most_recent_dm_thread_target(1) == row
        sql = " ".join(cursor.execute.call_args[0][0].split())
        assert "ORDER BY id DESC" in sql and "status=" not in sql

    def test_most_recent_dm_thread_target_is_none_with_no_history(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_most_recent_dm_thread_target
            assert get_most_recent_dm_thread_target(1) is None

    def test_most_recent_dm_thread_target_is_none_with_a_blank_profile_url(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one={"profile_url": "", "first_name": "Jane"})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_most_recent_dm_thread_target
            assert get_most_recent_dm_thread_target(1) is None
