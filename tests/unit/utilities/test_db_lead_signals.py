"""Unit tests for the lead_signals DB helpers (issue #483) — dedup insert, fail-safe existence
check, paginated inbox, partial updates, and the unactioned count.
"""

from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

_OUTREACH = "cqc_lem.platform.db.repositories.outreach"
_GET_CONN = "cqc_lem.platform.db.connection.get_db_connection"

pytestmark = pytest.mark.unit



def _mock_conn(fetch_row=None, fetch_all=None, lastrowid=7, side_effect=None):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = fetch_row
    cursor.fetchall.return_value = fetch_all or []
    cursor.lastrowid = lastrowid
    cursor.rowcount = 1
    if side_effect is not None:
        cursor.execute.side_effect = side_effect
    conn.cursor.return_value = cursor
    return conn, cursor


class TestInsertLeadSignal:
    def test_inserts_and_returns_the_id(self):
        conn, cursor = _mock_conn(lastrowid=7)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import LeadSignalChannel, LeadSignalSource, insert_lead_signal
            sid = insert_lead_signal(1, LeadSignalSource.POST_COMMENT, "lead:post:1:jane",
                                     person_name="Jane", score=55, matched_signals="pricing",
                                     channel=LeadSignalChannel.DM)
        assert sid == 7
        sql, params = cursor.execute.call_args[0]
        assert "INSERT IGNORE INTO lead_signals" in sql   # dedup is the UNIQUE(user_id, thread_key)
        assert params[1] == "post_comment" and params[2] == "dm"

    def test_duplicate_thread_returns_none(self):
        conn, _ = _mock_conn(lastrowid=0)   # INSERT IGNORE swallowed it
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import LeadSignalSource, insert_lead_signal
            assert insert_lead_signal(1, LeadSignalSource.DM, "lead:dm:thread:jane") is None

    def test_blank_key_is_refused(self):
        with patch(f"{_GET_CONN}") as get:
            from cqc_lem.utilities.db import LeadSignalSource, insert_lead_signal
            assert insert_lead_signal(1, LeadSignalSource.DM, "") is None
        get.assert_not_called()

    def test_score_is_clamped_into_the_tinyint_column(self):
        conn, cursor = _mock_conn()
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import LeadSignalSource, insert_lead_signal
            insert_lead_signal(1, LeadSignalSource.DM, "k", score=9999)
        assert cursor.execute.call_args[0][1][7] == 255

    def test_db_error_returns_none(self):
        conn, _ = _mock_conn(side_effect=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_OUTREACH}.log_info"):
            from cqc_lem.utilities.db import LeadSignalSource, insert_lead_signal
            assert insert_lead_signal(1, LeadSignalSource.DM, "k") is None


class TestHasLeadSignal:
    def test_true_when_present(self):
        conn, _ = _mock_conn(fetch_row=(1,))
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import has_lead_signal
            assert has_lead_signal(1, "k") is True

    def test_false_when_absent(self):
        conn, _ = _mock_conn(fetch_row=None)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import has_lead_signal
            assert has_lead_signal(1, "k") is False

    def test_blank_key_and_db_errors_fail_safe_to_skipping(self):
        from cqc_lem.utilities.db import has_lead_signal
        assert has_lead_signal(1, "") is True
        conn, _ = _mock_conn(side_effect=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn):
            assert has_lead_signal(1, "k") is True


class TestGetLeadSignals:
    def test_paginates_and_serializes_datetimes(self):
        from datetime import datetime
        rows = [{"id": 1, "created_at": datetime(2026, 7, 24, 12, 0), "updated_at": None}]
        conn, cursor = _mock_conn(fetch_row={"c": 1}, fetch_all=rows)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_lead_signals
            out = get_lead_signals(1, status_filter="new", page=2, page_size=10)
        assert out["total"] == 1 and out["page"] == 2
        assert out["signals"][0]["created_at"] == "2026-07-24T12:00:00"
        sql, params = cursor.execute.call_args[0]
        assert tuple(params[-2:]) == (10, 10)   # page_size, offset
        assert "status = %s" in sql

    def test_sort_order_asc(self):
        conn, cursor = _mock_conn(fetch_row={"c": 0}, fetch_all=[])
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_lead_signals
            get_lead_signals(1, sort_order="asc")
        assert "ORDER BY created_at ASC" in cursor.execute.call_args[0][0]

    def test_db_error_returns_an_empty_page(self):
        conn, _ = _mock_conn(side_effect=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_OUTREACH}.log_info"):
            from cqc_lem.utilities.db import get_lead_signals
            out = get_lead_signals(1)
        assert out == {"signals": [], "total": 0, "page": 1, "page_size": 25}


class TestGetLeadSignal:
    def test_returns_the_row_and_its_owner(self):
        conn, _ = _mock_conn(fetch_row={"id": 3, "user_id": 5})
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_lead_signal
            row = get_lead_signal(3)
        assert row["id"] == 3
        assert row["user_id"] == 5

    def test_missing_row_is_none(self):
        conn, _ = _mock_conn(fetch_row=None)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import get_lead_signal
            assert get_lead_signal(3) is None

    def test_db_error_returns_none(self):
        conn, _ = _mock_conn(side_effect=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_OUTREACH}.log_info"):
            from cqc_lem.utilities.db import get_lead_signal
            assert get_lead_signal(3) is None


class TestUpdateLeadSignal:
    def test_updates_only_the_supplied_fields(self):
        conn, cursor = _mock_conn()
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import LeadSignalStatus, update_lead_signal
            assert update_lead_signal(3, status=LeadSignalStatus.SENT) is True
        sql, params = cursor.execute.call_args[0]
        assert "status = %s" in sql and "draft_response" not in sql
        assert params == ("sent", 3)

    def test_draft_and_status_together(self):
        conn, cursor = _mock_conn()
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import LeadSignalStatus, update_lead_signal
            update_lead_signal(3, draft_response="hi", status=LeadSignalStatus.APPROVED)
        assert cursor.execute.call_args[0][1] == ("hi", "approved", 3)

    def test_nothing_to_update_is_a_no_op(self):
        with patch(f"{_GET_CONN}") as get:
            from cqc_lem.utilities.db import update_lead_signal
            assert update_lead_signal(3) is False
        get.assert_not_called()

    def test_db_error_returns_false(self):
        conn, _ = _mock_conn(side_effect=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn), patch(f"{_OUTREACH}.log_info"):
            from cqc_lem.utilities.db import LeadSignalStatus, update_lead_signal
            assert update_lead_signal(3, status=LeadSignalStatus.SENT) is False


class TestCountNewLeadSignals:
    def test_counts(self):
        conn, _ = _mock_conn(fetch_row=(4,))
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import count_new_lead_signals
            assert count_new_lead_signals(1) == 4

    def test_no_rows_is_zero(self):
        conn, _ = _mock_conn(fetch_row=None)
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import count_new_lead_signals
            assert count_new_lead_signals(1) == 0

    def test_db_error_is_zero(self):
        conn, _ = _mock_conn(side_effect=mysql.connector.Error("boom"))
        with patch(f"{_GET_CONN}", return_value=conn):
            from cqc_lem.utilities.db import count_new_lead_signals
            assert count_new_lead_signals(1) == 0
