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

    def test_get_due_returns_rows(self, fake_cursor):
        rows = [{"id": 1, "user_id": 1, "profile_url": "p", "first_name": "Jane",
                 "event_type": "connection_accepted", "next_step": 1}]
        conn, cursor = fake_cursor(fetch_all=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            import datetime

            from cqc_lem.utilities.db import get_due_followups
            out = get_due_followups(datetime.datetime(2026, 7, 4, 9, 0))
        assert out == rows
        assert "status='pending'" in cursor.execute.call_args[0][0] and "due_at <= %s" in cursor.execute.call_args[0][0]

    def test_mark_followup(self, fake_cursor):
        conn, cursor = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import mark_followup
            assert mark_followup(5, "sent") is True
        assert cursor.execute.call_args[0][1] == ("sent", 5)

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
