"""Unit tests for DM follow-up queue DB helpers."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _mock_conn(fetch_all=None, rowcount=1):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = fetch_all or []
    cursor.rowcount = rowcount
    conn.cursor.return_value = cursor
    return conn, cursor


class TestFollowupQueue:
    def test_enqueue_inserts_pending(self):
        conn, cursor = _mock_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import enqueue_followup
            import datetime
            ok = enqueue_followup(1, "https://x/in/jane", "Jane", "connection_accepted", 1,
                                  datetime.datetime(2026, 7, 4, 8, 0))
        assert ok is True
        assert "INSERT INTO dm_followups" in cursor.execute.call_args[0][0]

    def test_get_due_returns_rows(self):
        rows = [{"id": 1, "user_id": 1, "profile_url": "p", "first_name": "Jane",
                 "event_type": "connection_accepted", "next_step": 1}]
        conn, cursor = _mock_conn(fetch_all=rows)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_due_followups
            import datetime
            out = get_due_followups(datetime.datetime(2026, 7, 4, 9, 0))
        assert out == rows
        assert "status='pending'" in cursor.execute.call_args[0][0] and "due_at <= %s" in cursor.execute.call_args[0][0]

    def test_mark_followup(self):
        conn, cursor = _mock_conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import mark_followup
            assert mark_followup(5, "sent") is True
        assert cursor.execute.call_args[0][1] == ("sent", 5)

    def test_stop_followups_returns_count(self):
        conn, cursor = _mock_conn(rowcount=3)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import stop_followups_for_profile
            assert stop_followups_for_profile(1, "p") == 3
        assert "status='stopped'" in cursor.execute.call_args[0][0]
