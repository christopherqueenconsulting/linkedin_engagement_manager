import logging
import os
import sys
import datetime as DT
from logging.handlers import RotatingFileHandler
from typing import Optional

_LOG_LEVEL = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
# PostHog receives records at this level and above (default: ERROR)
_POSTHOG_MIN_LEVEL = getattr(logging, os.getenv("POSTHOG_LOG_LEVEL", "ERROR").upper(), logging.ERROR)

_today = DT.date.today()
LOGGING_FILENAME = "logs/cqc_lem_" + _today.strftime("%Y_%m_%d") + ".log"
os.makedirs("logs", exist_ok=True)


def _otlp_resource():
    """OTel Resource identifying LEM in PostHog Logs. Without this the provider defaults to
    'unknown_service' — set a real service.name so logs are filterable, tag the version from the
    deployed image tag, and use the container hostname as the instance id so each worker
    (web_app / celery_worker / *_selenium / *_content) is distinguishable."""
    from opentelemetry.sdk.resources import Resource
    attrs = {
        "service.name": os.getenv("OTEL_SERVICE_NAME", "cqc-lem"),
        "service.instance.id": os.getenv("HOSTNAME", "") or "unknown-instance",
    }
    version = os.getenv("IMAGE_TAG", "")
    if version:
        attrs["service.version"] = version
    env = os.getenv("DEPLOY_ENV", "")
    if env:
        attrs["deployment.environment"] = env
    return Resource.create(attrs)


def _build_posthog_handler(level: int) -> Optional[logging.Handler]:
    """Build an OTLP-backed LoggingHandler that ships logs to PostHog Logs."""
    api_key = os.getenv("POSTHOG_API_KEY", "")
    host = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com").rstrip("/")
    if not api_key:
        return None

    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    exporter = OTLPLogExporter(
        endpoint=f"{host}/i/v1/logs",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    provider = LoggerProvider(resource=_otlp_resource())
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    set_logger_provider(provider)

    return LoggingHandler(level=level, logger_provider=provider)


class _LevelFormatter(logging.Formatter):
    _fmt_debug = "[%(asctime)s %(filename)s->%(funcName)s():%(lineno)s] DEBUG: %(message)s"
    _fmt_info = "%(message)s"
    _fmt_warning = "WARNING [%(filename)s:%(lineno)s]: %(message)s"
    _fmt_error = "ERROR [%(filename)s->%(funcName)s():%(lineno)s]: %(message)s"
    _fmt_critical = "CRITICAL [%(filename)s->%(funcName)s():%(lineno)s]: %(message)s"

    _map = {
        logging.DEBUG: _fmt_debug,
        logging.INFO: _fmt_info,
        logging.WARNING: _fmt_warning,
        logging.ERROR: _fmt_error,
        logging.CRITICAL: _fmt_critical,
    }

    def format(self, record: logging.LogRecord) -> str:
        self._style._fmt = self._map.get(record.levelno, "%(levelname)s: %(message)s")
        return super().format(record)


# ── Build logger ─────────────────────────────────────────────────────────────

logger = logging.getLogger("cqc-lem")
logger.setLevel(_LOG_LEVEL)
logger.propagate = False  # don't double-log via root

_formatter = _LevelFormatter()

_file_handler = RotatingFileHandler(LOGGING_FILENAME, maxBytes=250_000_000, backupCount=10)
_file_handler.setFormatter(_formatter)
_file_handler.setLevel(_LOG_LEVEL)
logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_formatter)
_console_handler.setLevel(_LOG_LEVEL)
logger.addHandler(_console_handler)

_posthog_handler = _build_posthog_handler(_POSTHOG_MIN_LEVEL)
if _posthog_handler is not None:
    logger.addHandler(_posthog_handler)


# ── Internal helpers ─────────────────────────────────────────────────────────

_PRIMITIVE_TYPES = (bool, str, bytes, int, float)

# Errors reach PostHog twice on purpose (issue #648): the log stream keeps the message for CONTEXT,
# and the same exception is captured as a grouped $exception so alerting can move to issues. Off
# with POSTHOG_EXCEPTION_CAPTURE=false.
_CAPTURE_EXCEPTIONS = (os.getenv("POSTHOG_EXCEPTION_CAPTURE", "") or "").strip().lower() not in (
    "0", "false", "no", "off")


def _capture(exc: Optional[BaseException], message: str, level: str, context: dict) -> None:
    """Forward a logged exception to PostHog Error Tracking. Imported lazily because
    observability.py imports this module — and swallowing everything (including a caller's context
    key colliding with a named argument), since a telemetry failure must never turn a logged error
    into a raised one."""
    if exc is None or not _CAPTURE_EXCEPTIONS:
        return
    try:
        from cqc_lem.utilities.observability import capture_exception
        props = dict(context)
        props["log_message"] = message
        props["log_level"] = level
        capture_exception(exc, **props)
    except Exception:
        pass


def _extra(**kwargs) -> dict:
    # Structured-log backends (PostHog/OTel) only accept primitive attribute values, so coerce
    # anything else (e.g. an exception or WebElement passed as context) to str rather than
    # emitting an "Invalid type ... for attribute" warning and dropping the field.
    out = {}
    for k, v in kwargs.items():
        if v is None:
            continue
        out[k] = v if isinstance(v, _PRIMITIVE_TYPES) else str(v)
    return out


# ── Public API ────────────────────────────────────────────────────────────────

def myprint(message: str, debug: bool = False) -> None:
    """Backward-compatible shim. Prefer log_info / log_debug for new code."""
    if debug:
        logger.debug(message)
    else:
        logger.info(message)


def log_debug(message: str, **context) -> None:
    """Log at DEBUG level with optional structured context."""
    logger.debug(message, extra=_extra(**context))


def log_info(message: str, **context) -> None:
    """Log at INFO level with optional structured context."""
    logger.info(message, extra=_extra(**context))


def log_warning(
    message: str,
    exc: Optional[BaseException] = None,
    **context,
) -> None:
    """Log at WARNING level with optional structured context. Pass exc= to capture the
    exception's stack trace (via exc_info) instead of passing it as a raw attribute."""
    if exc is not None:
        logger.warning(message, exc_info=exc, extra=_extra(**context))
    else:
        logger.warning(message, extra=_extra(**context))


def log_error(
    message: str,
    exc: Optional[BaseException] = None,
    **context,
) -> None:
    """Log at ERROR level. Pass exc= to capture exception info and stack trace, and to file the
    exception as a grouped PostHog error-tracking issue."""
    if exc is not None:
        logger.error(message, exc_info=exc, extra=_extra(**context))
        _capture(exc, message, "ERROR", _extra(**context))
    else:
        logger.error(message, extra=_extra(**context))


def log_critical(
    message: str,
    exc: Optional[BaseException] = None,
    **context,
) -> None:
    """Log at CRITICAL level. Pass exc= to capture exception info and stack trace, and to file the
    exception as a grouped PostHog error-tracking issue."""
    if exc is not None:
        logger.critical(message, exc_info=exc, extra=_extra(**context))
        _capture(exc, message, "CRITICAL", _extra(**context))
    else:
        logger.critical(message, extra=_extra(**context))
