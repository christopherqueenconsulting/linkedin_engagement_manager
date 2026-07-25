"""Unit tests for the daily shipped-fix changelog/notify beat task (issue #502)."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_SHIPPED = "cqc_lem.utilities.feedback.shipped"


class TestAutoChangelogNotify:
    def test_summarises_the_pass(self):
        with patch(f"{_SHIPPED}.process_shipped_fixes",
                   return_value={"pull_requests": 3, "counts": {"notified": 2},
                                 "changelog": ["**Fixed:** X (#498)"]}) as process:
            from cqc_lem.app.run_scheduler import auto_changelog_notify
            result = auto_changelog_notify(hours=12)
        process.assert_called_once_with(hours=12)
        assert result == "Shipped-fix notify: 3 merged PR(s) — {'notified': 2}"

    def test_an_empty_pass_reads_as_nothing_to_do(self):
        with patch(f"{_SHIPPED}.process_shipped_fixes",
                   return_value={"pull_requests": 0, "counts": {}, "changelog": []}):
            from cqc_lem.app.run_scheduler import auto_changelog_notify
            assert auto_changelog_notify() == "Shipped-fix notify: 0 merged PR(s) — nothing to do"


class TestBeatSchedule:
    def test_task_runs_daily_after_the_survey_pass(self):
        from cqc_lem.app.my_celery import app
        entry = app.conf.beat_schedule["changelog-notify"]
        assert entry["task"] == "cqc_lem.app.run_scheduler.auto_changelog_notify"
        assert entry["schedule"].hour == {10}
        assert entry["schedule"].minute == {0}
