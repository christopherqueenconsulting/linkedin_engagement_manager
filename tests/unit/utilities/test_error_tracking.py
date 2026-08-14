"""Unit tests for PostHog error tracking — capture_exception + the log_error/log_critical
forwarding (issue #648).
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_OBS = "cqc_lem.utilities.observability"
_LOG = "cqc_lem.utilities.logger"


class TestCaptureException:
    def test_sends_exception_with_context_and_system_distinct_id(self):
        error = ValueError("boom")
        with patch(f"{_OBS}.posthog") as mock_ph, \
             patch(f"{_OBS}._current_task_context", return_value=(None, None)):
            mock_ph.disabled = False
            from cqc_lem.utilities.observability import capture_exception
            capture_exception(error, task_name="automate_commenting", source="unit")

        mock_ph.capture_exception.assert_called_once()
        args, kwargs = mock_ph.capture_exception.call_args
        assert args[0] is error
        assert kwargs["distinct_id"] == "system"
        assert kwargs["properties"]["task_name"] == "automate_commenting"
        assert kwargs["properties"]["source"] == "unit"

    def test_user_id_becomes_the_distinct_id(self):
        with patch(f"{_OBS}.posthog") as mock_ph, \
             patch(f"{_OBS}._current_task_context", return_value=(None, None)):
            mock_ph.disabled = False
            from cqc_lem.utilities.observability import capture_exception
            capture_exception(ValueError("boom"), user_id=42)

        _, kwargs = mock_ph.capture_exception.call_args
        assert kwargs["distinct_id"] == "42"
        assert kwargs["properties"]["user_id"] == 42

    def test_falls_back_to_the_running_celery_task_context(self):
        with patch(f"{_OBS}.posthog") as mock_ph, \
             patch(f"{_OBS}._current_task_context", return_value=("auto_check_scheduled_posts", 7)):
            mock_ph.disabled = False
            from cqc_lem.utilities.observability import capture_exception
            capture_exception(ValueError("boom"))

        _, kwargs = mock_ph.capture_exception.call_args
        assert kwargs["distinct_id"] == "7"
        assert kwargs["properties"]["task_name"] == "auto_check_scheduled_posts"

    def test_explicit_context_beats_the_ambient_task(self):
        with patch(f"{_OBS}.posthog") as mock_ph, \
             patch(f"{_OBS}._current_task_context", return_value=("ambient_task", 7)):
            mock_ph.disabled = False
            from cqc_lem.utilities.observability import capture_exception
            capture_exception(ValueError("boom"), user_id=1, task_name="explicit_task")

        _, kwargs = mock_ph.capture_exception.call_args
        assert kwargs["distinct_id"] == "1"
        assert kwargs["properties"]["task_name"] == "explicit_task"

    def test_none_context_values_are_dropped(self):
        with patch(f"{_OBS}.posthog") as mock_ph, \
             patch(f"{_OBS}._current_task_context", return_value=(None, None)):
            mock_ph.disabled = False
            from cqc_lem.utilities.observability import capture_exception
            capture_exception(ValueError("boom"), route=None, post_id=5)

        _, kwargs = mock_ph.capture_exception.call_args
        assert "route" not in kwargs["properties"]
        assert "task_name" not in kwargs["properties"]
        assert kwargs["properties"]["post_id"] == 5

    def test_no_op_when_posthog_is_disabled(self):
        with patch(f"{_OBS}.posthog") as mock_ph:
            mock_ph.disabled = True
            from cqc_lem.utilities.observability import capture_exception
            capture_exception(ValueError("boom"))

        mock_ph.capture_exception.assert_not_called()

    def test_never_raises_when_posthog_fails(self):
        with patch(f"{_OBS}.posthog") as mock_ph, \
             patch(f"{_OBS}._current_task_context", return_value=(None, None)), \
             patch(f"{_OBS}.log_warning") as mock_warn:
            mock_ph.disabled = False
            mock_ph.capture_exception.side_effect = RuntimeError("posthog down")
            from cqc_lem.utilities.observability import capture_exception
            capture_exception(ValueError("boom"))

        mock_warn.assert_called_once()


class TestLoggerForwarding:
    def test_log_error_with_exc_files_an_issue(self):
        error = ValueError("boom")
        with patch(f"{_OBS}.capture_exception") as mock_capture:
            from cqc_lem.utilities.logger import log_error
            log_error("Automation task failed", exc=error, user_id=3,
                      task_name="automate_commenting")

        mock_capture.assert_called_once()
        args, kwargs = mock_capture.call_args
        assert args[0] is error
        assert kwargs["user_id"] == 3
        assert kwargs["task_name"] == "automate_commenting"
        assert kwargs["log_level"] == "ERROR"
        assert kwargs["log_message"] == "Automation task failed"

    def test_log_critical_with_exc_files_an_issue(self):
        with patch(f"{_OBS}.capture_exception") as mock_capture:
            from cqc_lem.utilities.logger import log_critical
            log_critical("Suppression tripwire", exc=RuntimeError("nope"))

        assert mock_capture.call_args[1]["log_level"] == "CRITICAL"

    def test_log_error_without_exc_files_nothing(self):
        with patch(f"{_OBS}.capture_exception") as mock_capture:
            from cqc_lem.utilities.logger import log_error
            log_error("Something went wrong", user_id=3)

        mock_capture.assert_not_called()

    def test_log_warning_never_files(self):
        with patch(f"{_OBS}.capture_exception") as mock_capture:
            from cqc_lem.utilities.logger import log_warning
            log_warning("Perplexity unavailable", exc=RuntimeError("timeout"))

        mock_capture.assert_not_called()

    def test_non_primitive_context_is_coerced_before_capture(self):
        element = MagicMock()
        with patch(f"{_OBS}.capture_exception") as mock_capture:
            from cqc_lem.utilities.logger import log_error
            log_error("Selenium failed", exc=RuntimeError("stale"), element=element)

        assert isinstance(mock_capture.call_args[1]["element"], str)

    def test_capture_failure_never_breaks_logging(self):
        with patch(f"{_OBS}.capture_exception", side_effect=RuntimeError("posthog down")):
            from cqc_lem.utilities.logger import log_error
            log_error("Automation task failed", exc=ValueError("boom"))

    def test_kill_switch_stops_forwarding(self):
        # POSTHOG_EXCEPTION_CAPTURE=false resolves to this flag at import; patching it exercises the
        # same branch without reloading the module (which would re-add every log handler).
        with patch(f"{_LOG}._CAPTURE_EXCEPTIONS", False), \
             patch(f"{_OBS}.capture_exception") as mock_capture:
            from cqc_lem.utilities.logger import log_error
            log_error("Automation task failed", exc=ValueError("boom"))

        mock_capture.assert_not_called()


class TestTestSuiteNeverPublishesToPostHog:
    """The suite must never reach the real PostHog project, whatever is in `.env`.

    `tests/conftest.py` calls `load_dotenv()`, so a developer or VPS checkout with a production
    `POSTHOG_API_KEY` handed the whole suite a LIVE client. Every test exercising an error path
    published a real `$exception`: fixture messages ("boom", "db down", "fail") became PostHog
    error-tracking groups, and the daily error→issue cron filed them as GitHub issues that spent
    agent capacity on defects that did not exist.

    These assert the guard itself, not a behaviour built on top of it — without them the guard is
    one silent deletion away from restoring the leak, and nothing else in the suite would fail.
    """

    def test_posthog_sdk_is_disabled_under_pytest(self):
        import posthog
        assert posthog.disabled is True

    def test_module_disables_regardless_of_api_key(self):
        # The point of the guard: a configured key must NOT be enough to enable sending here.
        from cqc_lem.utilities.observability import _running_under_pytest
        assert _running_under_pytest() is True

    def test_exception_autocapture_is_disarmed_under_pytest(self):
        # Issue #1498. This hop used to ask only "is a key configured?", so a suite that inherited
        # the production POSTHOG_API_KEY armed the SDK's uncaught-exception excepthook — a global
        # sys.excepthook / threading.excepthook swap made mid-run, with only the client's own
        # disabled check between a fixture traceback and a real $exception.
        import posthog

        from cqc_lem.utilities import observability

        assert observability.EXCEPTION_AUTOCAPTURE_ENABLED is False
        assert posthog.enable_exception_autocapture is False

    def test_exception_autocapture_follows_the_sdk_disable_not_the_key(self):
        from cqc_lem.utilities.observability import _exception_autocapture_enabled

        with patch(f"{_OBS}.posthog") as mock_ph:
            mock_ph.disabled = False
            assert _exception_autocapture_enabled() is True

            # A key can be present and the SDK still disabled — under pytest it always is.
            mock_ph.disabled = True
            assert _exception_autocapture_enabled() is False

    def test_autocapture_kill_switch_still_wins_when_the_sdk_is_live(self, monkeypatch):
        from cqc_lem.utilities.observability import _exception_autocapture_enabled

        monkeypatch.setenv("POSTHOG_EXCEPTION_AUTOCAPTURE", "false")
        with patch(f"{_OBS}.posthog") as mock_ph:
            mock_ph.disabled = False
            assert _exception_autocapture_enabled() is False

    def test_capture_exception_is_a_no_op_while_disabled(self):
        # No `mock_ph.disabled = False` — unlike the send-path tests above, this asserts the
        # real default the suite runs under.
        with patch(f"{_OBS}.posthog") as mock_ph, \
             patch(f"{_OBS}._current_task_context", return_value=(None, None)):
            mock_ph.disabled = True
            from cqc_lem.utilities.observability import capture_exception
            capture_exception(ValueError("boom"), user_id=1)

        mock_ph.capture_exception.assert_not_called()
