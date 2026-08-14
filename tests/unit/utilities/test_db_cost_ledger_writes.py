"""Unit tests for the cost_ledger WRITE helpers (issue #490).

The read-only accessors the margin report uses live in test_db_cost_ledger.py.
"""

from datetime import date
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestInsertCostLedgerEntry:
    def test_inserts_row_with_defaults(self, fake_cursor):
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import CostCategory, insert_cost_ledger_entry
            assert insert_cost_ledger_entry("content", CostCategory.MEDIA, 0.25) is True

        sql, params = cur.execute.call_args[0]
        assert "INSERT INTO cost_ledger" in sql
        assert params[1:6] == ("content", "media", None, None, 0.25)
        assert isinstance(params[-1], date)  # incurred_on defaults to today
        conn.commit.assert_called_once()

    def test_keeps_sub_cent_precision(self, fake_cursor):
        """A cheap LLM tier rounds to zero at 2dp — the ledger must keep the signal."""
        conn, cur = fake_cursor()
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import insert_cost_ledger_entry
            insert_cost_ledger_entry("comment", "llm", 0.0000123, user_id=4, qty=1234.5678)

        params = cur.execute.call_args[0][1]
        assert params[0] == 4
        assert params[5] == 0.000012
        assert params[6] == 1234.5678

    def test_error_returns_false(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import insert_cost_ledger_entry
            assert insert_cost_ledger_entry("content", "media", 1.0) is False


class TestAccrueMonthlyFixedCosts:
    def test_no_accruals_is_noop(self):
        with patch("cqc_lem.platform.db.connection.get_db_connection") as get_conn:
            from cqc_lem.utilities.db import accrue_monthly_fixed_costs
            assert accrue_monthly_fixed_costs(date(2026, 7, 1), []) == 0
        get_conn.assert_not_called()

    def test_writes_missing_and_skips_existing(self, fake_cursor):
        conn, cur = fake_cursor()
        # First accrual already present for this period, second is new.
        cur.fetchone.side_effect = [(1,), None]
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import CostCategory, accrue_monthly_fixed_costs
            written = accrue_monthly_fixed_costs(date(2026, 7, 1), [
                {"user_id": 1, "category": CostCategory.PROXY, "usd": 3.0, "provider": "per_user_proxy"},
                {"user_id": 2, "category": CostCategory.INFRA, "usd": 1.5, "provider": "hosting", "qty": 1},
            ])

        assert written == 1
        inserts = [c for c in cur.execute.call_args_list if "INSERT INTO cost_ledger" in c[0][0]]
        assert len(inserts) == 1
        params = inserts[0][0][1]
        assert params[0] == 2 and params[2] == "infra" and params[4] == 1.5
        assert params[-1] == date(2026, 7, 1)

    def test_dedupe_check_is_null_safe(self, fake_cursor):
        """System rows carry user_id NULL, which `=` never matches — `<=>` does."""
        conn, cur = fake_cursor(fetch_one=None)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import accrue_monthly_fixed_costs
            accrue_monthly_fixed_costs(date(2026, 7, 1), [{"user_id": None, "category": "infra", "usd": 9.0}])
        assert "user_id <=> %s" in cur.execute.call_args_list[0][0][0]

    def test_error_returns_rows_written_so_far(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import accrue_monthly_fixed_costs
            assert accrue_monthly_fixed_costs(date(2026, 7, 1), [{"user_id": 1, "category": "proxy", "usd": 1}]) == 0


class TestGetUsersProxyConfig:
    def test_empty_input_skips_query(self):
        with patch("cqc_lem.platform.db.connection.get_db_connection") as get_conn:
            from cqc_lem.utilities.db import get_users_proxy_config
            assert get_users_proxy_config([]) == []
        get_conn.assert_not_called()

    def test_returns_proxy_and_country(self, fake_cursor):
        rows = [{"id": 1, "proxy_url": "http://p:3128", "country": "US"},
                {"id": 2, "proxy_url": None, "country": None}]
        conn, cur = fake_cursor(fetch_all=rows)
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_users_proxy_config
            got = get_users_proxy_config([1, 2])

        assert got == [{"user_id": 1, "proxy_url": "http://p:3128", "country": "US"},
                       {"user_id": 2, "proxy_url": None, "country": None}]
        sql, params = cur.execute.call_args[0]
        assert "IN (%s, %s)" in sql and params == (1, 2)

    def test_error_returns_empty(self, fake_cursor):
        import mysql.connector
        conn, cur = fake_cursor()
        cur.execute.side_effect = mysql.connector.Error(msg="boom")
        with patch("cqc_lem.platform.db.connection.get_db_connection", return_value=conn):
            from cqc_lem.utilities.db import get_users_proxy_config
            assert get_users_proxy_config([1]) == []
