"""DB layer for network activation (issue #623): engager connection-degree capture, connection
request failure reasons, and the outreach-funnel backlog counter.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


class TestEngagerConnectionDegree:
    def test_degree_is_written_and_coalesced(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_engager
            assert upsert_engager(1, "Jane Doe", "https://x/in/jane", connection_degree="2nd") is True
        sql, params = cur.execute.call_args[0]
        assert "connection_degree" in sql
        # A later sighting with no badge must not erase a degree we already know.
        assert "connection_degree=COALESCE(VALUES(connection_degree), connection_degree)" in sql
        assert params[3] == "2nd"

    def test_missing_degree_is_stored_as_null(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import upsert_engager
            upsert_engager(1, "Jane Doe", "https://x/in/jane")
        assert cur.execute.call_args[0][1][3] is None

    def test_candidates_expose_the_degree(self, fake_cursor):
        rows = [{"person_name": "Jane", "person_profile_url": "https://x/in/jane",
                 "connection_degree": "1st", "occurred_at": None}]
        conn, cur = fake_cursor(fetch_all=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_engager_candidates
            out = get_engager_candidates(1)
        assert "connection_degree" in cur.execute.call_args[0][0]
        assert out[0]["connection_degree"] == "1st"


class TestConnectionRequestFailureReason:
    def test_failure_reason_is_stored_and_truncated(self, fake_cursor):
        conn, cur = fake_cursor()
        cur.rowcount = 1
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import ConnectionRequestStatus, update_connection_request_status
            assert update_connection_request_status(3, ConnectionRequestStatus.FAILED,
                                                    failure_reason="x" * 900) is True
        sql, params = cur.execute.call_args[0]
        assert "failure_reason" in sql
        assert len(params[1]) == 512

    def test_a_status_change_without_a_reason_clears_the_old_one(self, fake_cursor):
        conn, cur = fake_cursor()
        cur.rowcount = 1
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import ConnectionRequestStatus, update_connection_request_status
            update_connection_request_status(3, ConnectionRequestStatus.SENT)
        assert cur.execute.call_args[0][1][1] is None

    def test_failure_reason_is_selected_for_the_review_ui(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one={"id": 3})
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_connection_request
            get_connection_request(3)
        assert "failure_reason" in cur.execute.call_args[0][0]


class TestOpenOutreachTargetCount:
    def test_counts_pending_and_approved_only(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=(4,))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_open_outreach_targets
            assert count_open_outreach_targets(1) == 4
        sql = cur.execute.call_args[0][0]
        assert "'pending','approved'" in sql and "stage <> 'completed'" in sql

    def test_zero_when_the_query_fails(self):
        import mysql.connector
        conn = MagicMock()
        cur = MagicMock()
        cur.execute.side_effect = mysql.connector.Error("no such table")
        conn.cursor.return_value = cur
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import count_open_outreach_targets
            assert count_open_outreach_targets(1) == 0
