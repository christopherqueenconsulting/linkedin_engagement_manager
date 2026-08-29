"""Unit tests for connection-request DB helpers (issue #398)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestConnectionRequestDb:
    def test_insert_returns_id(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=42)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import insert_connection_request
            got = insert_connection_request(1, "https://x/in/jane", message="hi", recipient_name="Jane")
        assert got == 42
        assert "INSERT INTO connection_requests" in cur.execute.call_args[0][0]

    def test_get_approved_filters_status(self, fake_cursor):
        conn, cur = fake_cursor(fetch_all=[(1, 5)], lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_approved_connection_requests
            rows = get_approved_connection_requests()
        assert rows == [(1, 5)]
        sql = cur.execute.call_args[0][0]
        assert "status = 'approved'" in sql
        assert "ORDER BY created_at ASC" in sql

    def test_get_orphaned(self, fake_cursor):
        from datetime import datetime, timedelta, timezone
        conn, cur = fake_cursor(fetch_all=[(9, 5)], lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_orphaned_connection_requests
            rows = get_orphaned_connection_requests(lookback_hours=2)
        assert rows == [(9, 5)]
        sql, params = cur.execute.call_args[0]
        assert "status = 'sending'" in sql and "updated_at <= %s" in sql
        assert params[0] <= datetime.now(timezone.utc) - timedelta(hours=2) + timedelta(seconds=5)

    def test_count_invites_sent_today(self, fake_cursor):
        # Combined daily budget (owner review): counted from the immutable ENGAGED/SUCCESS invite logs
        # (which cover BOTH reactive and proactive sends), not from connection_requests.updated_at.
        conn, cur = fake_cursor(lastrowid=7, fetch_one=(4,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import CONNECTION_REQUEST_SENT_MESSAGE, count_invites_sent_today
            assert count_invites_sent_today(1) == 4
        sql, params = cur.execute.call_args[0]
        assert "FROM logs" in sql and "action_type=%s" in sql and "message=%s" in sql and "CURDATE()" in sql
        assert params == (1, "engaged", "success", CONNECTION_REQUEST_SENT_MESSAGE)

    def test_list_returns_pagination_shape(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7, fetch_one={"c": 3})
        cur.fetchall.return_value = [{"id": 1, "created_at": None, "status": "pending"}]
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_connection_requests
            out = get_connection_requests(1, status_filter="pending", page=1, page_size=25)
        assert out["total"] == 3 and out["page"] == 1 and len(out["requests"]) == 1

    def test_update_status(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import ConnectionRequestStatus, update_connection_request_status
            assert update_connection_request_status(7, ConnectionRequestStatus.SENT) is True
        assert "UPDATE connection_requests SET status" in cur.execute.call_args[0][0]

    def test_partial_update_builds_only_provided_fields(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_connection_request
            assert update_connection_request(7, message="new note") is True
        sql = cur.execute.call_args[0][0]
        assert "message = %s" in sql and "recipient_profile_url" not in sql

    def test_update_status_change_clears_failure_reason(self, fake_cursor):
        # Issue #1735 — a retried/re-approved row must not keep showing yesterday's failure reason.
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import ConnectionRequestStatus, update_connection_request
            assert update_connection_request(7, status=ConnectionRequestStatus.APPROVED) is True
        sql = cur.execute.call_args[0][0]
        assert "status = %s" in sql and "failure_reason = NULL" in sql

    def test_update_noop_when_nothing_provided(self):
        from cqc_lem.utilities.db import update_connection_request
        assert update_connection_request(7) is False

    def test_get_user_id_helper(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one={"id": 7, "user_id": 1, "recipient_profile_url": "u",
                                     "recipient_name": None, "message": None, "status": "pending",
                                     "created_at": None, "updated_at": None}, lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_connection_request_user_id
            assert get_connection_request_user_id(7) == 1
