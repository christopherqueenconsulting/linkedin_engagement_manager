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

    def test_insert_persists_recipient_email(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=42)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import insert_connection_request
            insert_connection_request(1, "https://x/in/jane", recipient_email="jane@example.com")
        sql, params = cur.execute.call_args[0]
        assert "recipient_email" in sql and "jane@example.com" in params

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
        assert "recipient_email IS NOT NULL AS has_recipient_email" in cur.execute.call_args_list[1][0][0]

    def test_dispatch_read_selects_recipient_email_without_exposing_it_to_list_reads(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one={"id": 1, "recipient_email": "jane@example.com"})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_connection_request
            assert get_connection_request(1)["recipient_email"] == "jane@example.com"
        sql = cur.execute.call_args[0][0]
        assert "recipient_email IS NOT NULL" not in sql
        assert sql.endswith("recipient_email FROM connection_requests WHERE id = %s")

    def test_update_status(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import ConnectionRequestStatus, update_connection_request_status
            assert update_connection_request_status(7, ConnectionRequestStatus.SENT) is True
        assert "UPDATE connection_requests SET status" in cur.execute.call_args[0][0]

    def test_update_status_to_terminal_clears_recipient_email(self, fake_cursor):
        # Issue #1836 — the bounded-exposure half of the storage decision: an email held for a
        # request that just went SENT/FAILED/CANCELED will never be used again.
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import ConnectionRequestStatus, update_connection_request_status
            assert update_connection_request_status(7, ConnectionRequestStatus.FAILED) is True
        assert "recipient_email = NULL" in cur.execute.call_args[0][0]

    def test_update_status_to_non_terminal_leaves_recipient_email_alone(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import ConnectionRequestStatus, update_connection_request_status
            assert update_connection_request_status(7, ConnectionRequestStatus.APPROVED) is True
        assert "recipient_email" not in cur.execute.call_args[0][0]

    @pytest.mark.parametrize("status", ["sent", "FAILED", "canceled"])
    def test_terminal_status_strings_clear_recipient_email(self, fake_cursor, status):
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_connection_request_status
            assert update_connection_request_status(7, status) is True
        assert "recipient_email = NULL" in cur.execute.call_args[0][0]

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

    def test_update_can_set_recipient_email(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import update_connection_request
            assert update_connection_request(7, recipient_email="jane@example.com") is True
        sql, params = cur.execute.call_args[0]
        assert "recipient_email = %s" in sql and "jane@example.com" in params

    def test_status_change_to_terminal_also_clears_recipient_email(self, fake_cursor):
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import ConnectionRequestStatus, update_connection_request
            assert update_connection_request(7, status=ConnectionRequestStatus.FAILED) is True
        sql = cur.execute.call_args[0][0]
        assert "recipient_email = NULL" in sql

    def test_a_freshly_supplied_email_wins_over_the_terminal_clear(self, fake_cursor):
        # Two `recipient_email = ...` clauses in one UPDATE is invalid SQL — the explicit value wins.
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import ConnectionRequestStatus, update_connection_request
            assert update_connection_request(7, status=ConnectionRequestStatus.FAILED,
                                             recipient_email="jane@example.com") is True
        sql = cur.execute.call_args[0][0]
        assert sql.count("recipient_email") == 1
        assert "recipient_email = %s" in sql

    def test_reviving_to_approved_with_a_fresh_email_keeps_it_set(self, fake_cursor):
        # Issue #1881 — the revival this backs is `failed -> approved` directly (never through a
        # terminal status), so this asserts the OTHER half of "a freshly supplied email wins": here
        # there is no terminal clause to fight in the first place, and the address must still land.
        conn, cur = fake_cursor(lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import ConnectionRequestStatus, update_connection_request
            assert update_connection_request(7, status=ConnectionRequestStatus.APPROVED,
                                             recipient_email="jane@example.com") is True
        sql, params = cur.execute.call_args[0]
        assert "recipient_email = NULL" not in sql
        assert "recipient_email = %s" in sql and "jane@example.com" in params

    def test_get_user_id_helper(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one={"id": 7, "user_id": 1, "recipient_profile_url": "u",
                                     "recipient_name": None, "message": None, "status": "pending",
                                     "created_at": None, "updated_at": None}, lastrowid=7)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_connection_request_user_id
            assert get_connection_request_user_id(7) == 1


class TestRecordConnectionRequestAttempt:
    """Issue #1814 — attempt ceiling.

    The scanner/send task must call this ONLY for a dispatch that actually reached LinkedIn; the
    hold/cap/throttle defers never call it (covered in tests/unit/app/test_connection_requests.py,
    where the non-calls are asserted).
    """

    def test_below_ceiling_defers_back_to_approved(self, fake_cursor):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_MAX_ATTEMPTS
        conn, cursor = fake_cursor(fetch_one=(1,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_connection_request_attempt
            terminal, attempts = record_connection_request_attempt(7, "no Connect button")
        assert (terminal, attempts) == (False, 1)
        sql, params = cursor.execute.call_args_list[0][0]
        # status is assigned BEFORE the increment, so this test reads the pre-increment count.
        assert sql.index("status = IF") < sql.index("attempts = attempts + 1")
        # (caller-forced terminal, ceiling) — the caller did not force one here (issue #1813).
        assert params[:2] == (0, CONNECTION_REQUEST_MAX_ATTEMPTS)

    def test_a_caller_forced_terminal_retires_the_row_on_this_attempt(self, fake_cursor):
        """Issue #1813 — a PROVEN-unreachable target has nothing to learn from two more sessions.

        The ceiling exists to stop guessing about a target failing for reasons we cannot read. An
        out-of-network profile offering nothing but Follow is not a guess, and the retirement must
        land in the SAME statement as the attempt so the two can never separate.
        """
        conn, cursor = fake_cursor(fetch_one=(1,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_connection_request_attempt
            terminal, attempts = record_connection_request_attempt(7, "Follow only", terminal=True)
        assert (terminal, attempts) == (True, 1)
        assert cursor.execute.call_args_list[0][0][1][0] == 1
        # ONE write, not an attempt followed by a separate retirement.
        assert sum(1 for call in cursor.execute.call_args_list
                   if "UPDATE connection_requests" in call[0][0]) == 1

    def test_goes_terminal_at_the_attempt_cap(self, fake_cursor):
        from cqc_lem.utilities.db import CONNECTION_REQUEST_MAX_ATTEMPTS
        conn, cursor = fake_cursor(fetch_one=(CONNECTION_REQUEST_MAX_ATTEMPTS,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_connection_request_attempt
            terminal, attempts = record_connection_request_attempt(7, "no Connect button")
        assert (terminal, attempts) == (True, CONNECTION_REQUEST_MAX_ATTEMPTS)

    def test_an_unmatched_request_reports_no_attempts(self, fake_cursor):
        conn, cursor = fake_cursor()
        cursor.rowcount = 0
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_connection_request_attempt
            assert record_connection_request_attempt(999, "gone") == (False, 0)

    def test_going_terminal_at_the_ceiling_also_clears_recipient_email(self, fake_cursor):
        # Issue #1836 — the same bounded-exposure rule as update_connection_request_status: an
        # email held for a target that just exhausted its attempts will never be used again.
        from cqc_lem.utilities.db import CONNECTION_REQUEST_MAX_ATTEMPTS
        conn, cursor = fake_cursor(fetch_one=(CONNECTION_REQUEST_MAX_ATTEMPTS,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import record_connection_request_attempt
            record_connection_request_attempt(7, "no Connect button")
        sql = cursor.execute.call_args_list[0][0][0]
        assert "recipient_email = IF" in sql
