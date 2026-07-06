"""Unit tests for scheduled-DM DB helpers (issue #306)."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _conn(fetch_row=None, fetchall=None, lastrowid=7):
    conn = MagicMock(); cur = MagicMock()
    cur.fetchone.return_value = fetch_row
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = 1
    cur.lastrowid = lastrowid
    conn.cursor.return_value = cur
    return conn, cur


class TestScheduledDmDb:
    def test_insert_returns_id(self):
        from datetime import datetime
        conn, cur = _conn(lastrowid=42)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import insert_scheduled_dm
            got = insert_scheduled_dm(1, "https://x/in/jane", "hi", datetime(2026, 8, 1, 9))
        assert got == 42
        assert "INSERT INTO scheduled_dms" in cur.execute.call_args[0][0]

    def test_get_due_filters_approved(self):
        conn, cur = _conn(fetchall=[(1, MagicMock(), 5)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_due_scheduled_dms
            rows = get_due_scheduled_dms(post_time_delta_minutes=20)
        assert len(rows) == 1
        assert "status = 'approved'" in cur.execute.call_args[0][0]

    def test_list_returns_pagination_shape(self):
        conn, cur = _conn()
        cur.fetchone.return_value = {"c": 3}
        cur.fetchall.return_value = [{"id": 1, "scheduled_time": None, "status": "pending"}]
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_scheduled_dms
            out = get_scheduled_dms(1, status_filter="pending", page=1, page_size=25)
        assert out["total"] == 3 and out["page"] == 1 and len(out["dms"]) == 1

    def test_update_status(self):
        conn, cur = _conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_scheduled_dm_status, ScheduledDmStatus
            assert update_scheduled_dm_status(7, ScheduledDmStatus.SENT) is True
        assert "UPDATE scheduled_dms SET status" in cur.execute.call_args[0][0]

    def test_partial_update_builds_only_provided_fields(self):
        conn, cur = _conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_scheduled_dm
            assert update_scheduled_dm(7, message="new body") is True
        sql = cur.execute.call_args[0][0]
        assert "message = %s" in sql and "recipient_profile_url" not in sql

    def test_update_noop_when_nothing_provided(self):
        from cqc_lem.utilities.db import update_scheduled_dm
        assert update_scheduled_dm(7) is False
