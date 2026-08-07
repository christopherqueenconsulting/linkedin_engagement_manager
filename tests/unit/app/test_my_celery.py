"""Unit tests for cqc_lem.app.my_celery CloudWatch guard logic."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.app.my_celery"


# ---------------------------------------------------------------------------
# get_queue_metric
# ---------------------------------------------------------------------------

class TestGetQueueMetric:
    def test_returns_zero_when_aws_region_is_none(self):
        """get_queue_metric must return 0 immediately when AWS_REGION is not set."""
        with patch(f"{_MOD}.AWS_REGION", None), \
             patch(f"{_MOD}.get_cloudwatch_client") as mock_cw:
            from cqc_lem.app.my_celery import get_queue_metric

            result = get_queue_metric()

        assert result == 0
        mock_cw.assert_not_called()

    def test_queries_cloudwatch_when_aws_region_is_set(self):
        """get_queue_metric calls CloudWatch when AWS_REGION is configured."""
        mock_cw_client = MagicMock()
        mock_cw_client.get_metric_statistics.return_value = {
            "Datapoints": [{"Maximum": 3, "Timestamp": "2026-01-01T00:00:00Z"}]
        }

        with patch(f"{_MOD}.AWS_REGION", "us-east-1"), \
             patch(f"{_MOD}.get_cloudwatch_client", return_value=mock_cw_client):
            from cqc_lem.app.my_celery import get_queue_metric

            result = get_queue_metric()

        assert result == 3
        mock_cw_client.get_metric_statistics.assert_called_once()

    def test_returns_zero_when_no_datapoints(self):
        """get_queue_metric returns 0 when CloudWatch has no datapoints."""
        mock_cw_client = MagicMock()
        mock_cw_client.get_metric_statistics.return_value = {"Datapoints": []}

        with patch(f"{_MOD}.AWS_REGION", "us-east-1"), \
             patch(f"{_MOD}.get_cloudwatch_client", return_value=mock_cw_client):
            from cqc_lem.app.my_celery import get_queue_metric

            result = get_queue_metric()

        assert result == 0

    def test_returns_zero_on_cloudwatch_exception(self):
        """get_queue_metric returns 0 and logs error when CloudWatch raises."""
        with patch(f"{_MOD}.AWS_REGION", "us-east-1"), \
             patch(f"{_MOD}.get_cloudwatch_client", side_effect=Exception("network error")):
            from cqc_lem.app.my_celery import get_queue_metric

            result = get_queue_metric()

        assert result == 0


# ---------------------------------------------------------------------------
# update_queue_length_metric
# ---------------------------------------------------------------------------

class TestUpdateQueueLengthMetric:
    def _make_celery_app_mock(self, queue_len: int = 5) -> MagicMock:
        """Return a mock Celery app whose pool.acquire() context manager yields a Redis client."""
        redis_client = MagicMock()
        redis_client.llen.return_value = queue_len

        channel = MagicMock()
        channel.client = redis_client

        conn = MagicMock()
        conn.default_channel = channel

        pool = MagicMock()
        pool.acquire.return_value.__enter__ = MagicMock(return_value=conn)
        pool.acquire.return_value.__exit__ = MagicMock(return_value=False)

        app_mock = MagicMock()
        app_mock.pool = pool
        return app_mock

    def test_skips_cloudwatch_when_aws_region_is_none(self):
        """No CloudWatch call when AWS_REGION is None — no region error spammed in logs."""
        app_mock = self._make_celery_app_mock(queue_len=2)

        with patch(f"{_MOD}.AWS_REGION", None), \
             patch(f"{_MOD}.app", app_mock), \
             patch(f"{_MOD}.get_cloudwatch_client") as mock_cw:
            from cqc_lem.app.my_celery import update_queue_length_metric

            result = update_queue_length_metric(sender=MagicMock())

        assert result == 2
        mock_cw.assert_not_called()

    def test_publishes_metric_when_aws_region_is_set(self):
        """Metric is published to CloudWatch when AWS_REGION is configured."""
        app_mock = self._make_celery_app_mock(queue_len=7)
        mock_cw_client = MagicMock()

        with patch(f"{_MOD}.AWS_REGION", "us-east-1"), \
             patch(f"{_MOD}.app", app_mock), \
             patch(f"{_MOD}.get_cloudwatch_client", return_value=mock_cw_client):
            from cqc_lem.app.my_celery import update_queue_length_metric

            result = update_queue_length_metric(sender=MagicMock())

        assert result == 7
        mock_cw_client.put_metric_data.assert_called_once()

    def test_logs_error_but_does_not_raise_on_cloudwatch_failure(self):
        """CloudWatch errors are caught and logged; the function still returns total_tasks."""
        app_mock = self._make_celery_app_mock(queue_len=3)

        with patch(f"{_MOD}.AWS_REGION", "us-east-1"), \
             patch(f"{_MOD}.app", app_mock), \
             patch(f"{_MOD}.get_cloudwatch_client", side_effect=Exception("timeout")):
            from cqc_lem.app.my_celery import update_queue_length_metric

            result = update_queue_length_metric(sender=MagicMock())

        assert result == 3
