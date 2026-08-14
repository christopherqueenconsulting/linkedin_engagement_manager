"""Unit tests for get_dashboard_counts (SQL-aggregate dashboard stats)."""

import datetime
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestGetDashboardCounts:
    def test_aggregates_returned(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=(4, 1, 37))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_dashboard_counts
            out = get_dashboard_counts(1, datetime.datetime(2026, 7, 6))
        assert out == {"scheduled_this_week": 4, "pending_review": 1, "posted_total": 37}
        # single aggregate query, not a paginated fetch
        assert "SUM(" in cur.execute.call_args[0][0] and "LIMIT" not in cur.execute.call_args[0][0]

    def test_tz_aware_week_start_coerced_to_naive(self, fake_cursor):
        conn, cur = fake_cursor(fetch_one=(0, 0, 0))
        aware = datetime.datetime(2026, 7, 6, tzinfo=datetime.timezone.utc)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_dashboard_counts
            get_dashboard_counts(1, aware)
        # the week_start param passed to SQL must be tz-naive (avoids naive/aware compare error)
        params = cur.execute.call_args[0][1]
        passed_week_start = params[2]
        assert passed_week_start.tzinfo is None

    def test_null_sums_become_zero(self, fake_cursor):
        conn, _ = fake_cursor(fetch_one=(None, None, None))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_dashboard_counts
            assert get_dashboard_counts(1, datetime.datetime(2026, 7, 6)) == {
                "scheduled_this_week": 0, "pending_review": 0, "posted_total": 0}
