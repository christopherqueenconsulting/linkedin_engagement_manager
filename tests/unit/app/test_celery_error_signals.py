"""Unit tests for the Celery task_failure / task_retry error-tracking handlers (issue #648)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.app.my_celery"


def _sender(name: str = "cqc_lem.app.run_automation.automate_commenting"):
    return SimpleNamespace(name=name)


class TestTaskFailure:
    def test_captures_the_exception_with_task_and_user_context(self):
        error = RuntimeError("selenium died")
        with patch(f"{_MOD}.capture_exception") as mock_capture:
            from cqc_lem.app.my_celery import on_task_failure
            on_task_failure(task_id="abc-123", exception=error, sender=_sender(),
                            kwargs={"user_id": 5})

        mock_capture.assert_called_once()
        args, kwargs = mock_capture.call_args
        assert args[0] is error
        assert kwargs["user_id"] == 5
        assert kwargs["task_id"] == "abc-123"
        assert kwargs["task_name"] == "cqc_lem.app.run_automation.automate_commenting"
        assert kwargs["source"] == "celery.task_failure"

    def test_system_task_without_a_user_still_files(self):
        with patch(f"{_MOD}.capture_exception") as mock_capture:
            from cqc_lem.app.my_celery import on_task_failure
            on_task_failure(task_id="abc", exception=ValueError("x"), sender=_sender(), kwargs={})

        assert mock_capture.call_args[1]["user_id"] is None

    @pytest.mark.parametrize("kwargs", [None, {"user_id": None}, {"user_id": "not-an-int"}, "junk"])
    def test_unusable_user_id_is_dropped_not_raised(self, kwargs):
        with patch(f"{_MOD}.capture_exception") as mock_capture:
            from cqc_lem.app.my_celery import on_task_failure
            on_task_failure(task_id="abc", exception=ValueError("x"), sender=_sender(),
                            kwargs=kwargs)

        assert mock_capture.call_args[1]["user_id"] is None

    def test_string_user_id_is_coerced(self):
        with patch(f"{_MOD}.capture_exception") as mock_capture:
            from cqc_lem.app.my_celery import on_task_failure
            on_task_failure(task_id="abc", exception=ValueError("x"), sender=_sender(),
                            kwargs={"user_id": "9"})

        assert mock_capture.call_args[1]["user_id"] == 9


class TestTaskRetry:
    def test_captures_the_reason_when_it_is_an_exception(self):
        reason = ConnectionError("429 from LinkedIn")
        request = SimpleNamespace(id="req-1", kwargs={"user_id": 2}, retries=1)
        with patch(f"{_MOD}.capture_exception") as mock_capture:
            from cqc_lem.app.my_celery import on_task_retry
            on_task_retry(request=request, reason=reason, sender=_sender())

        args, kwargs = mock_capture.call_args
        assert args[0] is reason
        assert kwargs["user_id"] == 2
        assert kwargs["task_id"] == "req-1"
        assert kwargs["retries"] == 1
        assert kwargs["source"] == "celery.task_retry"

    @pytest.mark.parametrize("reason", [None, "rate limited", 42])
    def test_non_exception_reason_files_nothing(self, reason):
        with patch(f"{_MOD}.capture_exception") as mock_capture:
            from cqc_lem.app.my_celery import on_task_retry
            on_task_retry(request=SimpleNamespace(id="r", kwargs={}, retries=0), reason=reason,
                          sender=_sender())

        mock_capture.assert_not_called()

    def test_missing_request_does_not_raise(self):
        with patch(f"{_MOD}.capture_exception") as mock_capture:
            from cqc_lem.app.my_celery import on_task_retry
            on_task_retry(request=None, reason=ValueError("x"), sender=_sender())

        assert mock_capture.call_args[1]["user_id"] is None


class TestSignalsAreConnected:
    def test_failure_and_retry_handlers_are_wired_to_celery(self):
        from celery.signals import task_failure, task_retry

        from cqc_lem.app.my_celery import on_task_failure, on_task_retry

        # weak=False, so the receiver tuples hold the function itself rather than a weakref.
        assert on_task_failure in [receiver for _, receiver in task_failure.receivers]
        assert on_task_retry in [receiver for _, receiver in task_retry.receivers]
