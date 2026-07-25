"""Unit tests for the structured logger module."""

import logging
import os
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_logger_module():
    """Re-import logger in isolation so module-level setup reruns cleanly."""
    import importlib
    import cqc_lem.utilities.logger as mod
    importlib.reload(mod)
    return mod


# ---------------------------------------------------------------------------
# _build_posthog_handler (OTLP-based PostHog Logs integration)
# ---------------------------------------------------------------------------

class TestBuildPostHogHandler:
    def test_returns_none_when_no_api_key(self):
        from cqc_lem.utilities.logger import _build_posthog_handler

        with patch.dict(os.environ, {"POSTHOG_API_KEY": ""}, clear=False):
            result = _build_posthog_handler(logging.ERROR)

        assert result is None

    def test_returns_logging_handler_when_key_is_set(self):
        from opentelemetry.sdk._logs import LoggingHandler
        from cqc_lem.utilities.logger import _build_posthog_handler

        env = {"POSTHOG_API_KEY": "phc_testtoken", "POSTHOG_HOST": "https://us.i.posthog.com"}
        with patch.dict(os.environ, env, clear=False):
            result = _build_posthog_handler(logging.ERROR)

        assert isinstance(result, LoggingHandler)

    def test_handler_respects_requested_level(self):
        from cqc_lem.utilities.logger import _build_posthog_handler

        env = {"POSTHOG_API_KEY": "phc_testtoken", "POSTHOG_HOST": "https://us.i.posthog.com"}
        with patch.dict(os.environ, env, clear=False):
            result = _build_posthog_handler(logging.WARNING)

        assert result is not None
        assert result.level == logging.WARNING

    def test_exporter_endpoint_uses_host_env_var(self):
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from cqc_lem.utilities.logger import _build_posthog_handler

        env = {
            "POSTHOG_API_KEY": "phc_testtoken",
            "POSTHOG_HOST": "https://eu.i.posthog.com",
        }
        captured_exporter: list[OTLPLogExporter] = []

        original_init = OTLPLogExporter.__init__

        def capturing_init(self, *args, **kwargs):
            captured_exporter.append(self)
            original_init(self, *args, **kwargs)

        with patch.dict(os.environ, env, clear=False), \
             patch.object(OTLPLogExporter, "__init__", capturing_init):
            _build_posthog_handler(logging.ERROR)

        assert captured_exporter, "OTLPLogExporter was not instantiated"
        assert captured_exporter[0]._endpoint == "https://eu.i.posthog.com/i/v1/logs"


# ---------------------------------------------------------------------------
# log_debug / log_info / log_warning
# ---------------------------------------------------------------------------

class TestLogLevelFunctions:
    def test_log_debug_calls_logger_debug(self):
        from cqc_lem.utilities import logger as mod

        with patch.object(mod.logger, "debug") as mock_debug:
            mod.log_debug("debug msg", user_id=7)

        mock_debug.assert_called_once()
        args, kwargs = mock_debug.call_args
        assert args[0] == "debug msg"
        assert kwargs["extra"]["user_id"] == 7

    def test_log_info_calls_logger_info(self):
        from cqc_lem.utilities import logger as mod

        with patch.object(mod.logger, "info") as mock_info:
            mod.log_info("info msg", task_name="my_task")

        mock_info.assert_called_once()
        assert mock_info.call_args[1]["extra"]["task_name"] == "my_task"

    def test_log_warning_calls_logger_warning(self):
        from cqc_lem.utilities import logger as mod

        with patch.object(mod.logger, "warning") as mock_warn:
            mod.log_warning("warn msg", post_id=99)

        mock_warn.assert_called_once()
        assert mock_warn.call_args[1]["extra"]["post_id"] == 99

    def test_extra_filters_out_none_values(self):
        from cqc_lem.utilities import logger as mod

        with patch.object(mod.logger, "info") as mock_info:
            mod.log_info("msg", user_id=None, task_name="t")

        extra = mock_info.call_args[1]["extra"]
        assert "user_id" not in extra
        assert extra["task_name"] == "t"


# ---------------------------------------------------------------------------
# log_error / log_critical
# ---------------------------------------------------------------------------

class TestLogError:
    def test_log_error_without_exc(self):
        from cqc_lem.utilities import logger as mod

        with patch.object(mod.logger, "error") as mock_err:
            mod.log_error("something broke", user_id=5)

        mock_err.assert_called_once_with("something broke", extra={"user_id": 5})

    def test_log_error_with_exc_passes_exc_info(self):
        from cqc_lem.utilities import logger as mod

        exc = ValueError("test error")
        with patch.object(mod.logger, "error") as mock_err:
            mod.log_error("error with exc", exc=exc, user_id=3)

        _, kwargs = mock_err.call_args
        assert kwargs["exc_info"] is exc
        assert kwargs["extra"]["user_id"] == 3

    def test_log_critical_with_exc(self):
        from cqc_lem.utilities import logger as mod

        exc = RuntimeError("fatal")
        with patch.object(mod.logger, "critical") as mock_crit:
            mod.log_critical("critical error", exc=exc)

        assert mock_crit.call_args[1]["exc_info"] is exc

    def test_log_critical_without_exc(self):
        from cqc_lem.utilities import logger as mod

        with patch.object(mod.logger, "critical") as mock_crit:
            mod.log_critical("critical msg", task_name="fatal_task")

        mock_crit.assert_called_once_with("critical msg", extra={"task_name": "fatal_task"})


# ---------------------------------------------------------------------------
# myprint (backward-compat shim)
# ---------------------------------------------------------------------------

class TestMyprint:
    def test_myprint_routes_to_info_by_default(self):
        from cqc_lem.utilities import logger as mod

        with patch.object(mod.logger, "info") as mock_info:
            mod.myprint("hello")

        mock_info.assert_called_once_with("hello")

    def test_myprint_debug_true_routes_to_debug(self):
        from cqc_lem.utilities import logger as mod

        with patch.object(mod.logger, "debug") as mock_debug:
            mod.myprint("verbose detail", debug=True)

        mock_debug.assert_called_once_with("verbose detail")

    def test_myprint_debug_false_does_not_call_debug(self):
        from cqc_lem.utilities import logger as mod

        with patch.object(mod.logger, "debug") as mock_debug, \
             patch.object(mod.logger, "info"):
            mod.myprint("normal message")

        mock_debug.assert_not_called()


# ---------------------------------------------------------------------------
# Logger configuration
# ---------------------------------------------------------------------------

class TestLoggerConfiguration:
    def test_logger_has_file_handler(self):
        from cqc_lem.utilities import logger as mod

        handler_types = [type(h).__name__ for h in mod.logger.handlers]
        assert "RotatingFileHandler" in handler_types

    def test_logger_has_posthog_handler_when_key_configured(self):
        from cqc_lem.utilities import logger as mod

        # LoggingHandler is present when POSTHOG_API_KEY is set in environment
        api_key = os.getenv("POSTHOG_API_KEY", "")
        handler_types = [type(h).__name__ for h in mod.logger.handlers]
        if api_key:
            assert "LoggingHandler" in handler_types
        else:
            assert "LoggingHandler" not in handler_types

    def test_logger_has_stream_handler(self):
        from cqc_lem.utilities import logger as mod

        handler_types = [type(h).__name__ for h in mod.logger.handlers]
        assert "StreamHandler" in handler_types

    def test_logger_does_not_propagate(self):
        from cqc_lem.utilities import logger as mod

        assert mod.logger.propagate is False

    def test_posthog_handler_level_matches_env(self):
        from cqc_lem.utilities import logger as mod

        ph_handlers = [h for h in mod.logger.handlers if type(h).__name__ == "LoggingHandler"]
        if not ph_handlers:
            return  # no key configured — handler absent, nothing to assert
        expected = getattr(logging, os.getenv("POSTHOG_LOG_LEVEL", "ERROR").upper(), logging.ERROR)
        assert ph_handlers[0].level == expected


# ---------------------------------------------------------------------------
# OTLP resource (service.name) — issue: logs were landing under 'unknown_service'
# ---------------------------------------------------------------------------

@patch.dict(os.environ, {"HOSTNAME": "celery_worker_selenium", "IMAGE_TAG": "v0.70.0", "DEPLOY_ENV": "prod"}, clear=False)
def test_otlp_resource_sets_service_name_and_instance():
    from cqc_lem.utilities.logger import _otlp_resource
    attrs = _otlp_resource().attributes
    assert attrs["service.name"] == "cqc-lem"
    assert attrs["service.instance.id"] == "celery_worker_selenium"
    assert attrs["service.version"] == "v0.70.0"
    assert attrs["deployment.environment"] == "prod"


@patch.dict(os.environ, {"OTEL_SERVICE_NAME": "cqc-lem-web"}, clear=False)
def test_otlp_resource_service_name_overridable():
    from cqc_lem.utilities.logger import _otlp_resource
    assert _otlp_resource().attributes["service.name"] == "cqc-lem-web"


def test_otlp_resource_defaults_when_env_absent():
    from cqc_lem.utilities.logger import _otlp_resource
    with patch.dict(os.environ, {}, clear=True):
        attrs = _otlp_resource().attributes
        assert attrs["service.name"] == "cqc-lem"
        assert attrs["service.instance.id"] == "unknown-instance"
        assert "service.version" not in attrs  # not set when IMAGE_TAG absent


def test_posthog_handler_provider_carries_the_resource():
    """The LoggerProvider must be built WITH the resource (regression: was LoggerProvider())."""
    import cqc_lem.utilities.logger as mod
    with patch.dict(os.environ, {"POSTHOG_API_KEY": "phc_x"}, clear=False), \
         patch("opentelemetry.sdk._logs.LoggerProvider") as LP, \
         patch("opentelemetry.sdk._logs.LoggingHandler"), \
         patch("opentelemetry.sdk._logs.export.BatchLogRecordProcessor"), \
         patch("opentelemetry.exporter.otlp.proto.http._log_exporter.OTLPLogExporter"), \
         patch("opentelemetry._logs.set_logger_provider"):
        mod._build_posthog_handler(logging.ERROR)
    assert "resource" in LP.call_args.kwargs
    assert LP.call_args.kwargs["resource"].attributes["service.name"] == "cqc-lem"
