"""Unit tests for the read-only cost_ledger accessors used by the margin report (issue #491)."""

from datetime import date
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

pytestmark = pytest.mark.unit


def _mock_conn(fetch_all=None, fetch_one=None, error=None, dictionary=False):
    conn, cur = MagicMock(), MagicMock()
    cur.fetchall.return_value = fetch_all if fetch_all is not None else []
    cur.fetchone.return_value = fetch_one
    if error:
        cur.execute.side_effect = mysql.connector.Error(error)
    conn.cursor.return_value = cur
    return conn, cur


class TestCostLedgerAvailable:
    def test_true_when_table_exists(self):
        conn, cur = _mock_conn(fetch_one=("cost_ledger",))
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import cost_ledger_available
            assert cost_ledger_available() is True
        assert "SHOW TABLES LIKE 'cost_ledger'" in cur.execute.call_args[0][0]

    def test_false_when_absent(self):
        conn, _ = _mock_conn(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import cost_ledger_available
            assert cost_ledger_available() is False

    def test_false_on_db_error(self):
        conn, _ = _mock_conn(error="connection lost")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import cost_ledger_available
            assert cost_ledger_available() is False


class TestGetCostRollup:
    def test_sums_by_feature_across_all_users(self):
        conn, cur = _mock_conn(fetch_all=[("content", 1.5), ("comment", 0.25)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_cost_rollup
            assert get_cost_rollup(date(2026, 7, 1), date(2026, 7, 25)) == {"content": 1.5,
                                                                           "comment": 0.25}
        sql, params = cur.execute.call_args[0]
        assert "SUM(usd)" in sql and "CAST(feature AS CHAR)" in sql and "GROUP BY rollup_key" in sql
        assert "user_id = %s" not in sql  # no user filter → shared/system rows included
        assert params == (date(2026, 7, 1), date(2026, 7, 25))

    def test_filters_by_user_when_given(self):
        conn, cur = _mock_conn(fetch_all=[("llm", 7.0)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_cost_rollup
            assert get_cost_rollup(date(2026, 7, 1), date(2026, 7, 25),
                                   group_by="category", user_id=5) == {"llm": 7.0}
        sql, params = cur.execute.call_args[0]
        assert "CAST(category AS CHAR)" in sql and "GROUP BY rollup_key" in sql
        assert "user_id = %s" in sql
        assert params == (date(2026, 7, 1), date(2026, 7, 25), 5)

    def test_numeric_dimension_is_cast_so_null_never_collapses_into_user_zero(self):
        conn, cur = _mock_conn(fetch_all=[("unknown", 3.0), ("7", 1.0)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_cost_rollup
            assert get_cost_rollup(date(2026, 7, 1), date(2026, 7, 25),
                                   group_by="user") == {"unknown": 3.0, "7": 1.0}
        sql = cur.execute.call_args[0][0]
        assert "COALESCE(CAST(user_id AS CHAR), 'unknown')" in sql
        assert "GROUP BY rollup_key" in sql

    def test_rejects_an_unknown_dimension_instead_of_interpolating_it(self):
        with patch("cqc_lem.platform.db.connection.get_db_connection") as gc:
            from cqc_lem.utilities.db import get_cost_rollup
            assert get_cost_rollup(date(2026, 7, 1), date(2026, 7, 25),
                                   group_by="feature; DROP TABLE users") == {}
        gc.assert_not_called()

    def test_empty_when_table_missing(self):
        conn, _ = _mock_conn(error="Table 'cost_ledger' doesn't exist")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_cost_rollup
            assert get_cost_rollup(date(2026, 7, 1), date(2026, 7, 25)) == {}

    def test_get_user_cost_groups_by_category_for_one_user(self):
        conn, cur = _mock_conn(fetch_all=[("llm", 2.0), ("proxy", 1.0)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_user_cost
            assert get_user_cost(9, date(2026, 7, 1), date(2026, 7, 25)) == {"llm": 2.0,
                                                                            "proxy": 1.0}
        sql, params = cur.execute.call_args[0]
        assert "CAST(category AS CHAR)" in sql and params[-1] == 9


class TestGetDailyCostTotals:
    def test_returns_one_total_per_day_keyed_by_iso_date(self):
        conn, cur = _mock_conn(fetch_all=[(date(2026, 7, 24), 4.0), (date(2026, 7, 25), 9.5)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_daily_cost_totals
            assert get_daily_cost_totals(date(2026, 7, 18), date(2026, 7, 25)) == {
                "2026-07-24": 4.0, "2026-07-25": 9.5}
        sql, params = cur.execute.call_args[0]
        assert "GROUP BY incurred_on" in sql and "SUM(usd)" in sql
        assert params == (date(2026, 7, 18), date(2026, 7, 25))

    def test_days_without_rows_are_absent_not_zero(self):
        # A ledger that started capturing mid-window must not report a fabricated $0 baseline.
        conn, _ = _mock_conn(fetch_all=[(date(2026, 7, 25), 9.5)])
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_daily_cost_totals
            assert get_daily_cost_totals(date(2026, 7, 18), date(2026, 7, 25)) == {"2026-07-25": 9.5}

    def test_empty_when_table_missing(self):
        conn, _ = _mock_conn(error="Table 'cost_ledger' doesn't exist")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_daily_cost_totals
            assert get_daily_cost_totals(date(2026, 7, 18), date(2026, 7, 25)) == {}


class TestGetMarginUsers:
    def test_returns_tier_status_and_signup_cohort(self):
        rows = [{"id": 1, "subscription_tier": "professional", "subscription_status": "active",
                 "cohort": "2026-06"}]
        conn, cur = _mock_conn(fetch_all=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_margin_users
            assert get_margin_users() == rows
        sql = cur.execute.call_args[0][0]
        assert "'active', 'past_due', 'trial'" in sql  # trials cost money → included at $0 MRR
        assert "trial_started_at" in sql

    def test_empty_on_db_error(self):
        conn, _ = _mock_conn(error="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_margin_users
            assert get_margin_users() == []
