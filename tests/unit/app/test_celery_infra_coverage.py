"""Coverage tests for Celery infra helpers: celeryconfig SQS setup, my_celery signal
handlers/queue metrics, and the AWS test task.

The queue-depth reads share one contract — anything but a live datapoint is zero — so they
are a parametrized table (issue #1216); the rest assert one-of-a-kind wiring and stay plain.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


class TestGetAwsSqs:
    def test_builds_client_and_returns_queue_url(self):
        from cqc_lem.app.celeryconfig import get_aws_sqs
        session = MagicMock()
        session.client.return_value.get_queue_url.return_value = {"QueueUrl": "https://sqs/q"}
        result = get_aws_sqs("celery", session)
        assert result == {"QueueUrl": "https://sqs/q"}
        session.client.assert_called_once_with(service_name="sqs")
        session.client.return_value.get_queue_url.assert_called_once_with(QueueName="celery")


class TestSetupAwsSqsConfig:
    def test_noop_without_aws_region(self, monkeypatch):
        import cqc_lem.app.celeryconfig as cc
        monkeypatch.delenv("AWS_REGION", raising=False)
        with patch.object(cc.boto3.session, "Session") as sess:
            cc.setup_aws_sqs_config()
        sess.assert_not_called()

    def test_configures_predefined_queue_from_sqs(self, monkeypatch):
        import cqc_lem.app.celeryconfig as cc
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_SQS_QUEUE_NAME", "celery")
        session = MagicMock()
        session.get_credentials.return_value = MagicMock(access_key="AK", secret_key="SK")
        session.region_name = "us-east-1"
        with patch.object(cc.boto3.session, "Session", return_value=session), \
             patch.object(cc, "get_aws_sqs",
                          return_value={"QueueUrl": "https://sqs/q"}) as sqs:
            cc.setup_aws_sqs_config()
        sqs.assert_called_once_with(queue_name="celery", session=session)

    def test_session_error_is_swallowed(self, monkeypatch, capsys):
        import cqc_lem.app.celeryconfig as cc
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        with patch.object(cc.boto3.session, "Session",
                          side_effect=RuntimeError("no credentials")):
            cc.setup_aws_sqs_config()  # must not raise
        assert "Error getting AWS session" in capsys.readouterr().out


_DATAPOINTS = [
    {"Timestamp": datetime(2026, 7, 6, 12, 0), "Maximum": 3},
    {"Timestamp": datetime(2026, 7, 6, 12, 5), "Maximum": 9},
]

# Every way of not getting a reading is the same reading: zero. Only a live datapoint is a
# depth, so the metric can never scale workers off a CloudWatch outage.
# (case id, AWS_REGION, what get_cloudwatch_client does, expected depth)
_QUEUE_METRIC_CASES = [
    ("no_region", None, None, 0),
    ("latest_datapoint", "us-east-1", {"Datapoints": _DATAPOINTS}, 9),
    ("no_datapoints", "us-east-1", {"Datapoints": []}, 0),
    ("cloudwatch_error", "us-east-1", RuntimeError("denied"), 0),
]


class TestGetQueueMetric:
    @pytest.mark.parametrize("case_id,region,statistics,expected",
                             _QUEUE_METRIC_CASES, ids=[c[0] for c in _QUEUE_METRIC_CASES])
    def test_reads_the_depth_or_zero(self, case_id, region, statistics, expected):
        import cqc_lem.app.my_celery as mc
        if isinstance(statistics, Exception):
            client = patch.object(mc, "get_cloudwatch_client", side_effect=statistics)
        else:
            cw = MagicMock()
            cw.get_metric_statistics.return_value = statistics
            client = patch.object(mc, "get_cloudwatch_client", return_value=cw)
        with patch.object(mc, "AWS_REGION", region), client:
            assert mc.get_queue_metric() == expected


class TestCelerySignalHandlers:
    def test_task_pre_and_postrun_track_duration(self):
        import cqc_lem.app.my_celery as mc
        task = MagicMock()
        task.name = "cqc_lem.test_task"
        with patch.object(mc, "track_task") as track:
            mc.on_task_prerun(task_id="tid-1", task=task)
            assert "tid-1" in mc._task_start_times
            mc.on_task_postrun(task_id="tid-1", task=task, state="SUCCESS")
        assert "tid-1" not in mc._task_start_times
        kwargs = track.call_args[1]
        assert kwargs["task_name"] == "cqc_lem.test_task"
        assert kwargs["success"] is True
        assert kwargs["duration_ms"] >= 0

    def test_postrun_failure_state_tracked_as_unsuccessful(self):
        import cqc_lem.app.my_celery as mc
        task = MagicMock()
        task.name = "cqc_lem.failing_task"
        with patch.object(mc, "track_task") as track:
            mc.on_task_postrun(task_id="missing-tid", task=task, state="FAILURE")
        assert track.call_args[1]["success"] is False

    def test_configure_posthog_sets_sync_mode(self):
        import posthog

        import cqc_lem.app.my_celery as mc
        old = getattr(posthog, "sync_mode", False)
        try:
            mc.configure_posthog_for_worker()
            assert posthog.sync_mode is True
        finally:
            posthog.sync_mode = old


class TestAwsTestCeleryTasks:
    def test_test_task_sleeps_and_reports(self):
        from cqc_lem.app.aws_test_celery_task import test_task
        with patch("cqc_lem.app.aws_test_celery_task.time.sleep") as sleep:
            result = test_task(3)
        sleep.assert_called_once_with(3)
        assert result == "Slept for 3 seconds"

    def test_get_my_profile_task_fetches_twice_and_quits(self):
        from cqc_lem.app.aws_test_celery_task import test_get_my_profile
        driver, wait = MagicMock(), MagicMock()
        _T = "cqc_lem.app.aws_test_celery_task"
        with patch(f"{_T}.get_user_password_pair_by_id",
                   return_value=("a@x.com", "pw")), \
             patch(f"{_T}.get_driver_wait_pair", return_value=(driver, wait)), \
             patch(f"{_T}.remove_linked_in_profile_by_user_id") as remove, \
             patch(f"{_T}.get_my_profile") as gmp, \
             patch(f"{_T}.quit_gracefully") as quit_g:
            test_get_my_profile(3)
        remove.assert_called_once_with(3)
        assert gmp.call_count == 2
        quit_g.assert_called_once_with(driver)

    def test_get_my_profile_task_swallows_errors_and_still_quits(self):
        from cqc_lem.app.aws_test_celery_task import test_get_my_profile
        driver, wait = MagicMock(), MagicMock()
        _T = "cqc_lem.app.aws_test_celery_task"
        with patch(f"{_T}.get_user_password_pair_by_id",
                   return_value=("a@x.com", "pw")), \
             patch(f"{_T}.get_driver_wait_pair", return_value=(driver, wait)), \
             patch(f"{_T}.remove_linked_in_profile_by_user_id",
                   side_effect=RuntimeError("db down")), \
             patch(f"{_T}.quit_gracefully") as quit_g:
            test_get_my_profile(3)  # must not raise
        quit_g.assert_called_once_with(driver)
