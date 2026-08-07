"""Unit tests for the daily survey beat task (issue #501)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_S = "cqc_lem.utilities.surveys"
_DB = "cqc_lem.utilities.db"


def _run(candidates, surveys, send_result=True):
    with patch(f"{_DB}.get_survey_candidate_user_ids", return_value=candidates), \
         patch(f"{_S}.next_survey_for_user", side_effect=surveys), \
         patch(f"{_S}.send_survey_prompt", return_value=send_result) as send:
        from cqc_lem.app.run_scheduler import auto_survey_prompts
        result = auto_survey_prompts()
    return result, send


class TestAutoSurveyPrompts:
    def test_emails_only_the_users_with_a_due_survey(self):
        survey = {"key": "nps_day3"}
        result, send = _run([1, 2, 3], [survey, None, survey])
        assert send.call_count == 2
        assert result == "Asked 2 of 3 subscriber(s)"

    def test_counts_only_emails_that_actually_went_out(self):
        result, _send = _run([1], [{"key": "nps_day3"}], send_result=False)
        assert result == "Asked 0 of 1 subscriber(s)"

    def test_no_candidates(self):
        result, send = _run([], [])
        assert result == "Asked 0 of 0 subscriber(s)"
        send.assert_not_called()

    def test_one_user_failing_does_not_stop_the_rest(self):
        with patch(f"{_DB}.get_survey_candidate_user_ids", return_value=[1, 2]), \
             patch(f"{_S}.next_survey_for_user",
                   side_effect=[RuntimeError("db down"), {"key": "nps_day3"}]), \
             patch(f"{_S}.send_survey_prompt", return_value=True) as send:
            from cqc_lem.app.run_scheduler import auto_survey_prompts
            result = auto_survey_prompts()
        assert send.call_count == 1
        assert result == "Asked 1 of 2 subscriber(s)"


class TestBeatSchedule:
    def test_task_is_registered_daily_after_the_onboarding_nudge(self):
        from cqc_lem.app.my_celery import app
        entry = app.conf.beat_schedule["survey-prompts"]
        assert entry["task"] == "cqc_lem.app.run_scheduler.auto_survey_prompts"
        assert entry["schedule"].hour == {9}
        assert entry["schedule"].minute == {45}
