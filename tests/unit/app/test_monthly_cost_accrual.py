"""Unit tests for the monthly proxy/infra accrual + daily LLM rollup tasks (issue #490)."""

from datetime import datetime, timezone

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_SCHED = "cqc_lem.app.run_scheduler"


def _users(*rows):
    return [{"user_id": uid, "proxy_url": proxy, "country": country} for uid, proxy, country in rows]


class TestMonthlyCostAccruals:
    def test_per_user_proxy_charges_full_rate(self):
        from cqc_lem.app.run_scheduler import _monthly_cost_accruals
        got = _monthly_cost_accruals(_users((1, "http://paid:3128", "US")), 12.0, 30.0, 0)
        assert got == [{"user_id": 1, "category": "proxy", "usd": 12.0,
                        "provider": "per_user_proxy", "qty": 1}]

    def test_regional_proxy_cost_is_split_across_its_users(self):
        from cqc_lem.app.run_scheduler import _monthly_cost_accruals
        with patch("cqc_lem.utilities.proxy.resolve_proxy",
                   side_effect=["http://us:3128", "http://us:3128", "http://gb:3128"]):
            got = _monthly_cost_accruals(_users((1, None, "US"), (2, None, "US"), (3, None, "GB")), 0, 30.0, 0)

        by_user = {a["user_id"]: a["usd"] for a in got}
        assert by_user == {1: 15.0, 2: 15.0, 3: 30.0}
        assert {a["provider"] for a in got} == {"region_proxy"}

    def test_users_without_any_proxy_accrue_nothing(self):
        from cqc_lem.app.run_scheduler import _monthly_cost_accruals
        with patch("cqc_lem.utilities.proxy.resolve_proxy", return_value=None):
            assert _monthly_cost_accruals(_users((1, None, None)), 12.0, 30.0, 0) == []

    def test_infra_is_amortized_across_active_users(self):
        from cqc_lem.app.run_scheduler import _monthly_cost_accruals
        with patch("cqc_lem.utilities.proxy.resolve_proxy", return_value=None):
            got = _monthly_cost_accruals(_users((1, None, None), (2, None, None), (3, None, None)), 0, 0, 60.0)

        assert [a["usd"] for a in got] == [20.0, 20.0, 20.0]
        assert {a["category"] for a in got} == {"infra"}

    def test_unset_rates_write_nothing(self):
        """No prices configured (the default) must not litter the ledger with $0 rows."""
        from cqc_lem.app.run_scheduler import _monthly_cost_accruals
        assert _monthly_cost_accruals(_users((1, "http://paid:3128", "US")), 0, 0, 0) == []


class TestEnvRate:
    def test_missing_and_malformed_are_zero(self, monkeypatch):
        from cqc_lem.app.run_scheduler import _env_rate
        monkeypatch.delenv("PROXY_COST_PER_USER_MONTH", raising=False)
        assert _env_rate("PROXY_COST_PER_USER_MONTH") == 0.0
        monkeypatch.setenv("PROXY_COST_PER_USER_MONTH", "cheap")
        assert _env_rate("PROXY_COST_PER_USER_MONTH") == 0.0
        monkeypatch.setenv("PROXY_COST_PER_USER_MONTH", "7.5")
        assert _env_rate("PROXY_COST_PER_USER_MONTH") == 7.5


class TestAutoAccrueMonthlyCosts:
    def test_accrues_for_the_current_month(self, monkeypatch):
        monkeypatch.setenv("INFRA_FIXED_MONTHLY", "40")
        monkeypatch.setenv("PROXY_COST_PER_USER_MONTH", "0")
        monkeypatch.setenv("REGION_PROXY_COST", "0")
        with patch(f"{_SCHED}.get_active_user_ids", return_value=[1, 2]), \
             patch("cqc_lem.utilities.db.get_users_proxy_config",
                   return_value=_users((1, None, None), (2, None, None))), \
             patch("cqc_lem.utilities.db.accrue_monthly_fixed_costs", return_value=2) as accrue:
            from cqc_lem.app.run_scheduler import auto_accrue_monthly_costs
            assert auto_accrue_monthly_costs.run() == 2

        period, accruals = accrue.call_args[0]
        assert period == datetime.now(timezone.utc).date().replace(day=1)
        assert [a["usd"] for a in accruals] == [20.0, 20.0]

    def test_no_active_users_writes_nothing(self):
        with patch(f"{_SCHED}.get_active_user_ids", return_value=[]), \
             patch("cqc_lem.utilities.db.accrue_monthly_fixed_costs") as accrue:
            from cqc_lem.app.run_scheduler import auto_accrue_monthly_costs
            assert auto_accrue_monthly_costs.run() == 0
        accrue.assert_not_called()


class TestAutoRollupLlmCosts:
    def test_delegates_to_the_flush(self):
        with patch("cqc_lem.utilities.observability.flush_llm_cost_rollup", return_value=7) as flush:
            from cqc_lem.app.run_scheduler import auto_rollup_llm_costs
            assert auto_rollup_llm_costs.run() == 7
        flush.assert_called_once_with()


class TestBeatSchedule:
    def test_cost_tasks_are_scheduled(self):
        from cqc_lem.app.my_celery import app
        schedule = app.conf.beat_schedule
        assert schedule["rollup-llm-costs"]["task"] == f"{_SCHED}.auto_rollup_llm_costs"
        assert schedule["accrue-monthly-costs"]["task"] == f"{_SCHED}.auto_accrue_monthly_costs"
