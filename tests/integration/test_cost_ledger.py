"""Integration test for the cost ledger (#490).

Drives every writer that feeds `cost_ledger` — a media render, the daily LLM rollup, and the
monthly proxy/infra accrual — into one stateful fake `cost_ledger` table, then reads it back
through the read-only accessors the margin report uses (`get_cost_rollup` / `get_user_cost` /
`get_daily_cost_totals`, #491). That is the acceptance criterion ("media/proxy/infra costs land in
the ledger") proven end to end, without needing a live MySQL container.
"""
from datetime import date

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.integration

_DB = "cqc_lem.utilities.db"
_OBS = "cqc_lem.utilities.observability"
_REDIS = "cqc_lem.utilities.linkedin.rate_limit._redis_client"

_INSERT_COLUMNS = ("user_id", "feature", "category", "provider", "model_tier", "usd", "qty",
                   "post_id", "task_name", "incurred_on")
_ACCRUAL_COLUMNS = ("user_id", "feature", "category", "provider", "usd", "qty", "incurred_on")

# The reporting window every read below uses. Media rows are stamped with the real UTC "today", so
# an open-ended window keeps the test independent of the day it runs on.
_FROM, _TO = date(2000, 1, 1), date(2100, 1, 1)

# `rollup_key` is aliased in SQL — map it back to the row field each dimension groups by.
_ROLLUP_KEYS = {"feature": "feature", "category": "category", "provider": "provider",
                "model_tier": "model_tier", "user_id": "user_id", "task_name": "task_name"}


class _FakeCursor:
    """Minimal MySQL-ish cursor over an in-memory `cost_ledger` store."""

    def __init__(self, rows):
        self.rows = rows
        self._result = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        params = list(params or ())
        if s.startswith("INSERT INTO cost_ledger"):
            columns = _INSERT_COLUMNS if "model_tier" in s else _ACCRUAL_COLUMNS
            self.rows.append(dict(zip(columns, params)))
        elif s.startswith("SELECT id FROM cost_ledger"):
            user_id, category, period = params
            self._result = [(1,) for r in self.rows
                            if r["user_id"] == user_id and r["category"] == category
                            and r["incurred_on"] == period]
        elif "GROUP BY rollup_key" in s:
            matched = self._between(self.rows, params[0], params[1])
            if "user_id = %s" in s:
                only_user = params[2]
                matched = [r for r in matched if r["user_id"] == only_user]
            column = next(c for c in _ROLLUP_KEYS if f"CAST({c} AS CHAR)" in s)
            totals = {}
            for row in matched:
                key = row.get(_ROLLUP_KEYS[column])
                totals[str(key) if key is not None else "unknown"] = (
                    totals.get(str(key) if key is not None else "unknown", 0.0) + row["usd"])
            self._result = list(totals.items())
        elif "GROUP BY incurred_on" in s:
            matched = self._between(self.rows, params[0], params[1])
            totals = {}
            for row in matched:
                totals[row["incurred_on"]] = totals.get(row["incurred_on"], 0.0) + row["usd"]
            self._result = sorted(totals.items())
        else:
            raise AssertionError(f"unexpected SQL: {s}")

    @staticmethod
    def _between(rows, start, end):
        return [r for r in rows if start <= r["incurred_on"] <= end]

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return self._result

    def close(self):
        pass


@pytest.fixture
def ledger():
    rows = []
    connection = MagicMock()
    connection.cursor.return_value = _FakeCursor(rows)
    with patch(f"{_DB}.get_db_connection", return_value=connection):
        yield rows


def _redis_with_bucket():
    client = MagicMock()
    client.scan_iter.return_value = iter([b"lem:cost:llm:2026-07-24", b"lem:cost:llm:2026-07-24:qty"])
    client.hgetall.side_effect = lambda key: (
        {b"1|content|lem-complex": b"0.0105", b"2|comment|lem-simple": b"0.0004"}
        if not key.endswith(":qty") else {b"1|content|lem-complex": b"1500"}
    )
    return client


class TestCostLedgerPipeline:
    def test_media_llm_and_fixed_costs_all_land_and_roll_up(self, ledger):
        from cqc_lem.app.run_scheduler import _monthly_cost_accruals
        from cqc_lem.utilities.db import (accrue_monthly_fixed_costs, get_cost_rollup,
                                          get_user_cost)
        from cqc_lem.utilities.observability import flush_llm_cost_rollup, track_media_cost

        # 1. A video render for user 1 (variable, per-post).
        with patch(f"{_OBS}.posthog"):
            track_media_cost("video", "runway", 0.25, user_id=1, post_id=42, qty=5,
                             model="gen4_turbo")

        # 2. Yesterday's accumulated LLM spend, collapsed to one row per user x feature x tier.
        with patch(_REDIS, return_value=_redis_with_bucket()):
            assert flush_llm_cost_rollup(today="2026-07-25") == 2

        # 3. This month's fixed costs: user 1 on a paid proxy, infra split across both users.
        period = date(2026, 7, 1)
        users = [{"user_id": 1, "proxy_url": "http://paid:3128", "country": "US"},
                 {"user_id": 2, "proxy_url": None, "country": None}]
        with patch("cqc_lem.utilities.proxy.resolve_proxy", return_value=None):
            accruals = _monthly_cost_accruals(users, proxy_rate=12.0, region_rate=0.0,
                                              infra_monthly=40.0)
        assert accrue_monthly_fixed_costs(period, accruals) == 3
        # Re-running the monthly task must not double-charge.
        assert accrue_monthly_fixed_costs(period, accruals) == 0

        assert len(ledger) == 6
        media_row = next(r for r in ledger if r["category"] == "media")
        assert media_row["qty"] == 5 and media_row["post_id"] == 42  # seconds rendered

        by_category = get_cost_rollup(_FROM, _TO, group_by="category")
        assert set(by_category) == {"media", "llm", "proxy", "infra"}
        assert by_category["proxy"] == 12.0
        assert by_category["infra"] == 40.0          # 20 + 20, fully amortized
        assert by_category["llm"] == pytest.approx(0.0109)

        # user 1: 0.25 media + 0.0105 llm + 12 proxy + 20 infra
        assert sum(get_user_cost(1, _FROM, _TO).values()) == pytest.approx(32.2605)
        assert sum(get_user_cost(2, _FROM, _TO).values()) == pytest.approx(20.0004)

        by_feature = get_cost_rollup(_FROM, _TO, group_by="feature")
        assert by_feature["content"] == pytest.approx(0.2605)
        assert by_feature["system"] == pytest.approx(52.0)   # proxy + infra accruals

    def test_reads_respect_the_reporting_window(self, ledger):
        from cqc_lem.utilities.db import (get_daily_cost_totals, get_user_cost,
                                          insert_cost_ledger_entry)

        insert_cost_ledger_entry("content", "media", 1.0, user_id=1, incurred_on=date(2026, 6, 30))
        insert_cost_ledger_entry("content", "media", 2.0, user_id=1, incurred_on=date(2026, 7, 15))
        insert_cost_ledger_entry("content", "media", 4.0, user_id=1, incurred_on=date(2026, 8, 1))

        assert get_user_cost(1, date(2026, 7, 1), date(2026, 7, 31)) == {"media": 2.0}
        assert get_user_cost(1, _FROM, _TO) == {"media": 7.0}
        # Days with no rows stay absent rather than reading as a fabricated $0 baseline.
        assert get_daily_cost_totals(date(2026, 7, 1), date(2026, 7, 31)) == {"2026-07-15": 2.0}
