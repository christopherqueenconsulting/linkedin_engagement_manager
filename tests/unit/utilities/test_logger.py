"""Unit tests for the structured logger module."""

import datetime
import logging
import os
from unittest.mock import patch

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


class TestWarningEscalation:
    """Recurring warnings become ERRORs so they can reach PostHog Error Tracking."""

    def test_below_threshold_is_unchanged(self):
        from cqc_lem.utilities import logger as mod

        with patch.object(mod.log_escalation, "note", return_value=None), \
             patch.object(mod.logger, "warning") as mock_warn, \
             patch.object(mod.logger, "error") as mock_err:
            mod.log_warning("still just a warning", post_id=1)

        mock_warn.assert_called_once()
        mock_err.assert_not_called()

    def test_escalated_warning_is_emitted_at_error(self):
        from cqc_lem.utilities import logger as mod

        record = {"count": 3, "fingerprint": "deadbeefcafe", "display": "boom",
                  "window": 86400, "origin": "m.f", "level": "WARNING"}
        with patch.object(mod.log_escalation, "note", return_value=record), \
             patch.object(mod.log_escalation, "escalate") as mock_esc, \
             patch.object(mod.logger, "warning") as mock_warn, \
             patch.object(mod.logger, "error") as mock_err:
            mod.log_warning("boom", post_id=1)

        mock_warn.assert_not_called()
        mock_err.assert_called_once()
        extra = mock_err.call_args[1]["extra"]
        assert extra["escalated_from"] == "WARNING"
        assert extra["occurrence_count"] == 3
        assert extra["log_fingerprint"] == "deadbeefcafe"
        mock_esc.assert_called_once()

    def test_escalation_failure_never_breaks_logging(self):
        from cqc_lem.utilities import logger as mod

        with patch.object(mod.log_escalation, "note", side_effect=RuntimeError("redis")), \
             patch.object(mod.logger, "warning") as mock_warn:
            mod.log_warning("boom")  # must not raise

        mock_warn.assert_called_once()  # falls back to the plain warning

    def test_capture_forwards_fingerprint(self):
        from cqc_lem.utilities import logger as mod

        with patch("cqc_lem.utilities.observability.capture_exception") as cap:
            mod._capture(ValueError("x"), "msg", "ERROR", {}, fingerprint="lem-log:abc")
        assert cap.call_args[1]["fingerprint"] == "lem-log:abc"


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
# myprint — retired, and it has to stay retired
# ---------------------------------------------------------------------------

class TestTheMyprintShimIsGone:
    """The shim hid the LEVEL at the call site, and level is a routing decision here.

    `myprint` picked INFO or DEBUG from a `debug=` flag. `log_warning` escalates on repeat and files
    a grouped `$exception`, so which level a message carries decides whether it pages someone — that
    has to be legible in the function name, not in an argument.

    Ruff bans the import (TID251), but ruff's gate is a ratchet and not yet required, so the ban
    alone would not fail a build. These two do.
    """

    def test_the_shim_is_not_importable(self):
        from cqc_lem.utilities import logger as mod

        assert not hasattr(mod, "myprint")

    def test_no_source_file_still_calls_it(self):
        """A reintroduced shim would otherwise only surface as a NameError on the branch that logs."""
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[3] / "src" / "cqc_lem"
        call = re.compile(r"^[^#]*\bmyprint\s*\(")
        offenders = [
            f"{path.relative_to(root)}:{n}"
            for path in root.rglob("*.py")
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if call.match(line)
        ]
        # my_celery.py keeps one inside a triple-quoted block of disabled code; it is text, not a
        # call, and the AST-based sweep correctly left it alone.
        offenders = [o for o in offenders if not o.startswith("app/my_celery.py")]
        assert offenders == [], f"myprint() called from: {offenders}"


# ---------------------------------------------------------------------------
# Logger configuration
# ---------------------------------------------------------------------------

class TestLoggerConfiguration:
    def test_logger_has_file_handler(self):
        from cqc_lem.utilities import logger as mod

        handler_types = [type(h).__name__ for h in mod.logger.handlers]
        assert "DatedRotatingFileHandler" in handler_types

    def test_file_handler_keeps_size_rotation_settings(self):
        from cqc_lem.utilities import logger as mod

        handler = next(h for h in mod.logger.handlers
                       if isinstance(h, mod.DatedRotatingFileHandler))
        assert handler.maxBytes == 250_000_000
        assert handler.backupCount == 10

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
# Dated file rotation (#1093) — the file name was frozen at process start
# ---------------------------------------------------------------------------

class TestDatedRotatingFileHandler:
    """The clock is injected, so a midnight boundary is a list of dates, not a wait."""

    @staticmethod
    def _handler(mod, days, tmp_path, monkeypatch):
        """A handler whose clock walks `days`, holding the last one once exhausted."""
        monkeypatch.chdir(tmp_path)
        os.makedirs(mod.LOG_DIR, exist_ok=True)
        ticks = list(days)
        handler = mod.DatedRotatingFileHandler(
            max_bytes=1_000_000, backup_count=3,
            clock=lambda: ticks[0] if len(ticks) == 1 else ticks.pop(0),
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        return handler

    @staticmethod
    def _record(message):
        return logging.LogRecord("cqc-lem", logging.INFO, __file__, 1, message, None, None)

    def test_path_is_the_existing_dated_name(self):
        from cqc_lem.utilities import logger as mod

        assert mod.dated_log_path(datetime.date(2026, 8, 5)) == os.path.join(
            "logs", "cqc_lem_2026_08_05.log")

    def test_construction_creates_no_zero_byte_file(self, tmp_path, monkeypatch):
        """A process that imports the logger and never logs must leave no dated file behind."""
        from cqc_lem.utilities import logger as mod

        self._handler(mod, [datetime.date(2026, 8, 6)], tmp_path, monkeypatch)

        assert list((tmp_path / "logs").iterdir()) == []

    def test_writes_to_todays_file(self, tmp_path, monkeypatch):
        from cqc_lem.utilities import logger as mod

        handler = self._handler(mod, [datetime.date(2026, 8, 5)], tmp_path, monkeypatch)
        handler.emit(self._record("hello"))
        handler.close()

        assert (tmp_path / "logs" / "cqc_lem_2026_08_05.log").read_text().strip() == "hello"

    def test_next_days_record_lands_in_the_next_days_file(self, tmp_path, monkeypatch):
        """The bug: a long-lived worker kept appending to the file it opened on day one."""
        from cqc_lem.utilities import logger as mod

        day1, day2 = datetime.date(2026, 8, 5), datetime.date(2026, 8, 6)
        handler = self._handler(mod, [day1, day1, day2], tmp_path, monkeypatch)
        handler.emit(self._record("before midnight"))
        handler.emit(self._record("after midnight"))
        handler.close()

        assert (tmp_path / "logs" / "cqc_lem_2026_08_05.log").read_text().strip() == "before midnight"
        assert (tmp_path / "logs" / "cqc_lem_2026_08_06.log").read_text().strip() == "after midnight"

    def test_a_day_with_no_records_gets_no_file(self, tmp_path, monkeypatch):
        """The empty siblings implied a rotation that was not happening — no record, no file."""
        from cqc_lem.utilities import logger as mod

        day1, day3 = datetime.date(2026, 8, 5), datetime.date(2026, 8, 7)
        handler = self._handler(mod, [day1, day3], tmp_path, monkeypatch)
        handler.emit(self._record("first record of the process"))
        handler.close()

        assert [p.name for p in (tmp_path / "logs").iterdir()] == ["cqc_lem_2026_08_07.log"]

    def test_size_rotation_still_applies_within_a_day(self, tmp_path, monkeypatch):
        from cqc_lem.utilities import logger as mod

        day = datetime.date(2026, 8, 5)
        monkeypatch.chdir(tmp_path)
        os.makedirs(mod.LOG_DIR, exist_ok=True)
        handler = mod.DatedRotatingFileHandler(max_bytes=32, backup_count=3, clock=lambda: day)
        handler.setFormatter(logging.Formatter("%(message)s"))
        for _ in range(4):
            handler.emit(self._record("x" * 30))
        handler.close()

        assert (tmp_path / "logs" / "cqc_lem_2026_08_05.log.1").exists()

    def test_clock_is_utc(self):
        """Midnight UTC is the boundary — a non-UTC host must not date the file locally."""
        from cqc_lem.utilities import logger as mod

        assert mod._utc_today() == datetime.datetime.now(datetime.timezone.utc).date()


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
