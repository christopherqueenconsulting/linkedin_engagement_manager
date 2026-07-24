"""Unit tests for connection-request DB helpers (issue #398)."""

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


class TestConnectionRequestDb:
    def test_insert_returns_id(self):
        conn, cur = _conn(lastrowid=42)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import insert_connection_request
            got = insert_connection_request(1, "https://x/in/jane", message="hi", recipient_name="Jane")
        assert got == 42
        assert "INSERT INTO connection_requests" in cur.execute.call_args[0][0]

    def test_get_approved_filters_status(self):
        conn, cur = _conn(fetchall=[(1, 5)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_approved_connection_requests
            rows = get_approved_connection_requests()
        assert rows == [(1, 5)]
        sql = cur.execute.call_args[0][0]
        assert "status = 'approved'" in sql
        assert "ORDER BY created_at ASC" in sql

    def test_get_orphaned(self):
        from datetime import datetime, timezone, timedelta
        conn, cur = _conn(fetchall=[(9, 5)])
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_orphaned_connection_requests
            rows = get_orphaned_connection_requests(lookback_hours=2)
        assert rows == [(9, 5)]
        sql, params = cur.execute.call_args[0]
        assert "status = 'sending'" in sql and "updated_at <= %s" in sql
        assert params[0] <= datetime.now(timezone.utc) - timedelta(hours=2) + timedelta(seconds=5)

    def test_count_invites_sent_today(self):
        conn, cur = _conn()
        cur.fetchone.return_value = (4,)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_invites_sent_today
            assert count_invites_sent_today(1) == 4
        sql, params = cur.execute.call_args[0]
        assert "FROM connection_requests" in sql and "status=%s" in sql and "CURDATE()" in sql
        assert params == (1, "sent")

    def test_list_returns_pagination_shape(self):
        conn, cur = _conn()
        cur.fetchone.return_value = {"c": 3}
        cur.fetchall.return_value = [{"id": 1, "created_at": None, "status": "pending"}]
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_connection_requests
            out = get_connection_requests(1, status_filter="pending", page=1, page_size=25)
        assert out["total"] == 3 and out["page"] == 1 and len(out["requests"]) == 1

    def test_update_status(self):
        conn, cur = _conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_connection_request_status, ConnectionRequestStatus
            assert update_connection_request_status(7, ConnectionRequestStatus.SENT) is True
        assert "UPDATE connection_requests SET status" in cur.execute.call_args[0][0]

    def test_partial_update_builds_only_provided_fields(self):
        conn, cur = _conn()
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_connection_request
            assert update_connection_request(7, message="new note") is True
        sql = cur.execute.call_args[0][0]
        assert "message = %s" in sql and "recipient_profile_url" not in sql

    def test_update_noop_when_nothing_provided(self):
        from cqc_lem.utilities.db import update_connection_request
        assert update_connection_request(7) is False

    def test_get_user_id_helper(self):
        conn, cur = _conn(fetch_row={"id": 7, "user_id": 1, "recipient_profile_url": "u",
                                     "recipient_name": None, "message": None, "status": "pending",
                                     "created_at": None, "updated_at": None})
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_connection_request_user_id
            assert get_connection_request_user_id(7) == 1
