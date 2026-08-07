"""Unit tests for get_dashboard_counts (SQL-aggregate dashboard stats)."""

import datetime
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_DB = "cqc_lem.utilities.db"


def _conn(row):
    conn = MagicMock(); cur = MagicMock()
    cur.fetchone.return_value = row
    conn.cursor.return_value = cur
    return conn, cur


class TestGetDashboardCounts:
    def test_aggregates_returned(self):
        conn, cur = _conn((4, 1, 37))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_dashboard_counts
            out = get_dashboard_counts(1, datetime.datetime(2026, 7, 6))
        assert out == {"scheduled_this_week": 4, "pending_review": 1, "posted_total": 37}
        # single aggregate query, not a paginated fetch
        assert "SUM(" in cur.execute.call_args[0][0] and "LIMIT" not in cur.execute.call_args[0][0]

    def test_tz_aware_week_start_coerced_to_naive(self):
        conn, cur = _conn((0, 0, 0))
        aware = datetime.datetime(2026, 7, 6, tzinfo=datetime.timezone.utc)
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_dashboard_counts
            get_dashboard_counts(1, aware)
        # the week_start param passed to SQL must be tz-naive (avoids naive/aware compare error)
        params = cur.execute.call_args[0][1]
        passed_week_start = params[2]
        assert passed_week_start.tzinfo is None

    def test_null_sums_become_zero(self):
        conn, _ = _conn((None, None, None))
        with patch(f"{_DB}.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_dashboard_counts
            assert get_dashboard_counts(1, datetime.datetime(2026, 7, 6)) == {
                "scheduled_this_week": 0, "pending_review": 0, "posted_total": 0}
