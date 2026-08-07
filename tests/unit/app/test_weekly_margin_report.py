"""Unit tests for the weekly margin-report beat task (issue #491)."""

from datetime import date
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_M = "cqc_lem.utilities.margin"


def _report():
    from cqc_lem.utilities.margin import build_margin_report
    return build_margin_report(
        date(2026, 7, 19), date(2026, 7, 25),
        [{"user_id": 1, "tier": "professional", "cohort": "2026-06",
          "cost_by_category": {"llm": 7.0}, "engagement": {}}])


class TestAutoWeeklyMarginReport:
    def test_collects_then_delivers_the_report(self):
        report = _report()
        with patch(f"{_M}.collect_margin_report", return_value=report) as collect, \
             patch(f"{_M}.send_weekly_margin_report",
                   return_value={"emailed": True, "tracked": True, "report": report}) as send:
            from cqc_lem.app.run_scheduler import auto_weekly_margin_report
            result = auto_weekly_margin_report()

        collect.assert_called_once_with(days=7)
        send.assert_called_once_with(report)
        assert "2026-07-19→2026-07-25" in result
        assert "emailed=True" in result and "tracked=True" in result

    def test_window_is_configurable(self):
        report = _report()
        with patch(f"{_M}.collect_margin_report", return_value=report) as collect, \
             patch(f"{_M}.send_weekly_margin_report",
                   return_value={"emailed": False, "tracked": True, "report": report}):
            from cqc_lem.app.run_scheduler import auto_weekly_margin_report
            auto_weekly_margin_report(days=30)

        collect.assert_called_once_with(days=30)


class TestBeatSchedule:
    def test_the_weekly_report_is_scheduled_on_mondays(self):
        from cqc_lem.app.my_celery import app
        entry = app.conf.beat_schedule["weekly-margin-report"]
        assert entry["task"] == "cqc_lem.app.run_scheduler.auto_weekly_margin_report"
        crontab = entry["schedule"]
        assert crontab.day_of_week == {1}  # Monday
        assert crontab.hour == {12} and crontab.minute == {0}
