"""Unit tests for the daily onboarding beat task (issue #500)."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_RS = "cqc_lem.app.run_scheduler"
_ON = "cqc_lem.utilities.onboarding"
_DB = "cqc_lem.utilities.db"


def _run(candidates, nudges, send_result=True):
    with patch(f"{_DB}.get_onboarding_candidate_user_ids", return_value=candidates), \
         patch(f"{_ON}.sync_onboarding_state", return_value={}) as sync, \
         patch(f"{_ON}.next_nudge_for_user", side_effect=nudges), \
         patch(f"{_ON}.send_onboarding_nudge", return_value=send_result) as send:
        from cqc_lem.app.run_scheduler import auto_onboarding_nudges
        result = auto_onboarding_nudges()
    return result, sync, send


class TestAutoOnboardingNudges:
    def test_syncs_every_candidate_and_sends_only_due_nudges(self):
        nudge = {"key": "connect_linkedin"}
        result, sync, send = _run([1, 2, 3], [nudge, None, nudge])
        assert sync.call_count == 3
        assert send.call_count == 2
        assert result == "Nudged 2 of 3 un-activated user(s)"

    def test_counts_only_emails_that_actually_went_out(self):
        result, _sync, _send = _run([1], [{"key": "connect_linkedin"}], send_result=False)
        assert result == "Nudged 0 of 1 un-activated user(s)"

    def test_no_candidates(self):
        result, _sync, send = _run([], [])
        assert result == "Nudged 0 of 0 un-activated user(s)"
        send.assert_not_called()

    def test_one_user_failing_does_not_stop_the_rest(self):
        with patch(f"{_DB}.get_onboarding_candidate_user_ids", return_value=[1, 2]), \
             patch(f"{_ON}.sync_onboarding_state", side_effect=[RuntimeError("db down"), {}]), \
             patch(f"{_ON}.next_nudge_for_user", return_value={"key": "connect_linkedin"}), \
             patch(f"{_ON}.send_onboarding_nudge", return_value=True) as send:
            from cqc_lem.app.run_scheduler import auto_onboarding_nudges
            result = auto_onboarding_nudges()
        assert send.call_count == 1
        assert result == "Nudged 1 of 2 un-activated user(s)"


class TestBeatSchedule:
    def test_task_is_registered_daily(self):
        from cqc_lem.app.my_celery import app
        entry = app.conf.beat_schedule["onboarding-nudges"]
        assert entry["task"] == "cqc_lem.app.run_scheduler.auto_onboarding_nudges"
