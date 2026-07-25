"""Unit tests for the weekly feature-tutorial beat task (issue #505)."""

import pytest
from unittest.mock import patch

pytestmark = pytest.mark.unit

_VT = "cqc_lem.utilities.marketing.video_tutorials"


def _record(**overrides):
    record = {"flow": "getting-started", "duration_seconds": 32.5, "total_usd": 0.021,
              "youtube_url": "https://www.youtube.com/watch?v=abc"}
    record.update(overrides)
    return record


class TestAutoProduceFeatureTutorial:
    def test_reports_the_produced_tutorial_and_its_cost(self):
        with patch(f"{_VT}.produce_tutorial", return_value=_record()) as produce:
            from cqc_lem.app.run_scheduler import auto_produce_feature_tutorial
            result = auto_produce_feature_tutorial()
        produce.assert_called_once_with(flow_key=None)
        assert "getting-started" in result and "$0.021" in result and "youtube=yes" in result

    def test_notes_when_the_render_was_not_published(self):
        with patch(f"{_VT}.produce_tutorial", return_value=_record(youtube_url=None)):
            from cqc_lem.app.run_scheduler import auto_produce_feature_tutorial
            assert "youtube=no" in auto_produce_feature_tutorial()

    def test_nothing_due_is_not_a_failure(self):
        with patch(f"{_VT}.produce_tutorial", return_value=None):
            from cqc_lem.app.run_scheduler import auto_produce_feature_tutorial
            assert auto_produce_feature_tutorial() == "No tutorial produced"

    def test_a_specific_flow_can_be_requested(self):
        with patch(f"{_VT}.produce_tutorial", return_value=_record(flow="content-studio")) as produce:
            from cqc_lem.app.run_scheduler import auto_produce_feature_tutorial
            auto_produce_feature_tutorial(flow_key="content-studio")
        produce.assert_called_once_with(flow_key="content-studio")

    @pytest.mark.parametrize("error", ["TutorialCaptureError", "TutorialGuardrailError",
                                       "TutorialRenderError"])
    def test_pipeline_failures_abort_the_task_without_raising(self, error):
        from cqc_lem.utilities.marketing import video_tutorials as vt
        exc = getattr(vt, error)("stale UI")
        with patch(f"{_VT}.produce_tutorial", side_effect=exc):
            from cqc_lem.app.run_scheduler import auto_produce_feature_tutorial
            result = auto_produce_feature_tutorial()
        assert result == "Aborted: stale UI"

    def test_an_unexpected_error_still_surfaces(self):
        with patch(f"{_VT}.produce_tutorial", side_effect=RuntimeError("disk full")):
            from cqc_lem.app.run_scheduler import auto_produce_feature_tutorial
            with pytest.raises(RuntimeError, match="disk full"):
                auto_produce_feature_tutorial()


class TestBeatSchedule:
    def test_runs_weekly_on_the_selenium_content_lane(self):
        from cqc_lem.app import celeryconfig
        from cqc_lem.app.my_celery import app
        entry = app.conf.beat_schedule["produce-feature-tutorial"]
        assert entry["task"] == "cqc_lem.app.run_scheduler.auto_produce_feature_tutorial"
        assert entry["schedule"].hour == {20}
        assert entry["schedule"].day_of_week == {3}  # Wednesday
        assert celeryconfig.task_routes[entry["task"]] == {"queue": "se_content"}
