"""Unit tests for the capacity-watch beat task and its Selenium instrumentation — issue #552."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_CA = "cqc_lem.utilities.capacity_alerts"


class TestAutoCapacityWatch:
    def test_a_clean_window_samples_but_delivers_nothing(self):
        report = {"alert_count": 0, "sample_count": 12, "skipped_checks": ["session_wait"]}
        with patch(f"{_CA}.collect_capacity_report", return_value=report) as collect, \
             patch(f"{_CA}.send_capacity_alerts") as send:
            from cqc_lem.app.run_scheduler import auto_capacity_watch
            result = auto_capacity_watch()
        collect.assert_called_once()
        send.assert_not_called()
        assert "Capacity ok" in result and "12 sample(s)" in result

    def test_a_breach_is_delivered_and_summarized(self):
        report = {"alert_count": 2, "critical_count": 1, "sample_count": 40, "skipped_checks": []}
        with patch(f"{_CA}.collect_capacity_report", return_value=report), \
             patch(f"{_CA}.send_capacity_alerts",
                   return_value={"alerts": 2, "tracked": True,
                                 "github": {"action": "filed", "issue": 601}}) as send:
            from cqc_lem.app.run_scheduler import auto_capacity_watch
            result = auto_capacity_watch()
        send.assert_called_once_with(report)
        assert "2 breach(es)" in result and "github=filed" in result


class TestBeatSchedule:
    def _schedule(self):
        from cqc_lem.app.my_celery import app
        return app.conf.beat_schedule

    def test_entry_points_at_a_real_task(self):
        import cqc_lem.app.run_scheduler as scheduler
        assert self._schedule()['capacity-watch']['task'] == \
            "cqc_lem.app.run_scheduler.auto_capacity_watch"
        assert hasattr(scheduler, "auto_capacity_watch")

    def test_samples_every_fifteen_minutes(self):
        # The window size (CAPACITY_WINDOW_SAMPLES=672 ≈ 7 days) assumes this cadence.
        assert self._schedule()['capacity-watch']['schedule'].minute == {0, 15, 30, 45}


class TestSeleniumInstrumentation:
    def test_session_acquisition_time_is_recorded(self):
        from cqc_lem.utilities import selenium_util
        with patch(f"{_CA}.record_session_wait") as record:
            selenium_util._record_session_wait(12.5)
        record.assert_called_once_with(12.5)

    def test_a_broken_monitor_never_costs_a_browser_session(self):
        from cqc_lem.utilities import selenium_util
        with patch(f"{_CA}.record_session_wait", side_effect=RuntimeError("redis down")):
            selenium_util._record_session_wait(1.0)  # must not raise

    def test_get_docker_driver_times_the_remote_call(self):
        from cqc_lem.utilities import selenium_util
        driver = MagicMock()
        with patch.object(selenium_util, "_wait_for_selenium_ready"), \
             patch.object(selenium_util.webdriver, "Remote", return_value=driver), \
             patch.object(selenium_util, "_record_session_wait") as record, \
             patch.object(selenium_util, "time") as clock:
            clock.time.side_effect = [100.0, 175.0]
            selenium_util.get_docker_driver(session_name="test")
        assert record.call_args[0][0] == pytest.approx(75.0)
