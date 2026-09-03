"""The ONE logger: level helpers, structured context, handlers, and the OTLP hop into PostHog Logs.

`print()` is never used in this codebase, and neither is the `myprint` shim that predated these
helpers — it delegated to `logger.info`/`logger.debug` and was retired once every one of its 713 call
sites had been rewritten to the level it already resolved to. Context travels as keyword args
(`user_id`, `post_id`, `task_name`, …) and is coerced to primitives before it leaves, because an
exception or a WebElement passed as context would otherwise be dropped by the backend rather than
logged.

**Once is a warning, repeatedly is a defect.** `log_warning` runs every message through
`log_escalation`: past the threshold the SAME warning is re-emitted at ERROR and filed as one grouped
`$exception`, which is what alerts and what the daily cron turns into a GitHub issue. So an expected
no-op must be logged at DEBUG — warning about it files a defect against working behaviour. The
escalation import, the capture hop and the escalation call are each guarded independently: a
telemetry failure must never turn a logged error into a raised one, and a partial deploy must degrade
to the pre-escalation behaviour rather than break every log call in the app.

A log line and a PostHog `$exception` are different products. Only `exc=` on `log_error` /
`log_critical` (or an escalated warning) files the second one.
"""

import datetime as DT
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Callable, Optional

_LOG_LEVEL = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
# PostHog receives records at this level and above (default: ERROR)
_POSTHOG_MIN_LEVEL = getattr(logging, os.getenv("POSTHOG_LOG_LEVEL", "ERROR").upper(), logging.ERROR)

LOG_DIR = "logs"
_LOG_FILE_MAX_BYTES = 250_000_000
_LOG_FILE_BACKUP_COUNT = 10
os.makedirs(LOG_DIR, exist_ok=True)


def _utc_today() -> DT.date:
    """The date a record belongs to, read in UTC.

    The VPS, the deploy log and every timestamp an incident is reconstructed from are UTC, so the
    file name has to roll on the same midnight they do.
    """
    return DT.datetime.now(DT.timezone.utc).date()


def dated_log_path(day: DT.date) -> str:
    """Path of the log file that a record written on `day` belongs in."""
    return os.path.join(LOG_DIR, f"cqc_lem_{day:%Y_%m_%d}.log")


class DatedRotatingFileHandler(RotatingFileHandler):
    """A size-rotating file handler that re-resolves its DATED file name per record.

    The name used to be frozen at import (issue #1093): a container started on Aug 5 kept appending
    to `cqc_lem_2026_08_05.log` for as long as it lived, so during triage "no log today" read as an
    outage. Opening is deferred as well, because the 0-byte `cqc_lem_2026_08_06.log` siblings that
    made rotation look broken were processes that imported the logger and then never logged.

    Size-based rotation within a day is unchanged — backups stay `<dated name>.log.1`.
    """

    def __init__(self, *, max_bytes: int, backup_count: int,
                 clock: Callable[[], DT.date] = _utc_today) -> None:
        """Open nothing yet — the first record decides both the date and the file.

        `clock` is injected so a midnight boundary is testable without waiting for one.
        """
        self._clock = clock
        self._current_day = clock()
        super().__init__(dated_log_path(self._current_day), maxBytes=max_bytes,
                         backupCount=backup_count, delay=True)

    def emit(self, record: logging.LogRecord) -> None:
        """Write `record` to TODAY's file, switching files first if the date has moved on.

        The switch is guarded the same way the base class guards its own write: a logging call must
        never raise into the caller, so a failed close (a full disk flushes on `close()`) reports
        through `handleError` and the record is still written.
        """
        try:
            self._switch_day_if_needed()
        except Exception:  # pragma: no cover - exercised via a raising stream in the tests
            self.handleError(record)
        super().emit(record)

    def _switch_day_if_needed(self) -> None:
        """Re-point at today's file across a midnight boundary.

        Only ever called from `emit()`, which `logging` already serialises under the handler lock,
        so the close-and-reopen cannot race another writer in this process.
        """
        day = self._clock()
        if day == self._current_day:
            return
        self._current_day = day
        self.baseFilename = os.path.abspath(dated_log_path(day))
        # Detach BEFORE closing: if the close raises, the handler must still be pointed at today's
        # file rather than stranded on a dead stream for yesterday's.
        stream, self.stream = self.stream, None  # FileHandler.emit reopens against baseFilename
        if stream is not None:
            stream.close()


def _otlp_resource():
    """OTel Resource identifying LEM in PostHog Logs. Without this the provider defaults to
    'unknown_service' — set a real service.name so logs are filterable, tag the version from the
    deployed image tag, and use the container hostname as the instance id so each worker
    (web_app / celery_worker / *_selenium / *_content) is distinguishable.
    """
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


def _running_under_pytest() -> bool:
    """True when the importing process is pytest — the ONE definition of that question.

    A test run reaches the real PostHog project through the process ENVIRONMENT, not just a `.env`
    file: `lem-agentd` loads `agent-pipeline/secrets.env` (which legitimately holds
    `POSTHOG_API_KEY` for the pipeline's own telemetry) as a systemd `EnvironmentFile`, and every
    pytest it spawns inherits it. `tests/conftest.py` also calls `load_dotenv()`, so a checkout
    whose `.env` carries a real key does the same thing.

    Both of LEM's PostHog hops read that key at import, so both have to refuse here. Issue #1451
    closed the `$exception` half in `observability.py` — a mocked DB cursor raising
    `mysql.connector.Error("db down")` had real production code catch it, call `log_error(exc=...)`
    and publish a genuine grouped exception that the daily error→issue cron filed as a GitHub issue
    (#1460). This handler is the other half: with a key present the suite shipped every ERROR
    record it provoked ("SMTP send failed for test@example.com") into PostHog Logs, where prod runs
    at `POSTHOG_LOG_LEVEL=WARNING` and the fixture noise buries the real warnings triage reads.

    Guarding here rather than by removing the key: the pipeline needs that key for its own
    telemetry, so the fix has to hold regardless of what the environment supplies.

    `sys.modules` rather than `PYTEST_CURRENT_TEST`, which pytest sets per-test — it is unset during
    collection and at module import, which is exactly when this runs.
    """
    return "pytest" in sys.modules


#: Env var an operator CLI sets on ITSELF to keep its run out of the production PostHog project.
TELEMETRY_MUTED_ENV = "LEM_TELEMETRY_MUTED"


def telemetry_muted() -> bool:
    """True when this process has declared itself not-production and must publish no telemetry.

    The sibling of `_running_under_pytest` for the other caller that reaches the real project
    through the process ENVIRONMENT rather than through intent: a `scripts/` operator CLI. The
    corpus samplers document that they need a database the agent worktree does not have, and
    `lem-agentd` hands every run it spawns a real `POSTHOG_API_KEY` (see `_running_under_pytest`)
    but no MySQL credentials — so one sampler run on the host published a genuine grouped
    `$exception` for its own documented "no database here" condition (`ProgrammingError: 1045
    Access denied for user 'lem_user'`), and the daily error→issue cron filed it as issue #1661.

    Every hop off `POSTHOG_API_KEY` answers it: the OTLP Logs handler below, `observability._emit`
    (so no ANALYTICS event lands either) and `observability.capture_exception`.

    An env var rather than an inferred "is this a script?": `scripts/` also holds tooling whose
    telemetry is the point (`posthog_annotate.py`, `benchmark_models.py`), and guessing would mute
    those too. `os.environ.setdefault` at the top of a CLI leaves `LEM_TELEMETRY_MUTED=0` as the
    operator's way back in when a run genuinely should be recorded.
    """
    raw = (os.getenv(TELEMETRY_MUTED_ENV) or "").strip().lower()
    return bool(raw) and raw not in ("0", "false", "no", "off")


def _build_posthog_handler(level: int) -> Optional[logging.Handler]:
    """Build an OTLP-backed LoggingHandler that ships logs to PostHog Logs.

    Returns None — no handler, nothing exported — when there is no key, under pytest whatever the
    key says (see `_running_under_pytest`), or in a self-muted process (`telemetry_muted`). The
    refusal lives in the builder rather than at the single call site below so a future second
    caller inherits it instead of re-opening the leak.
    """
    api_key = os.getenv("POSTHOG_API_KEY", "")
    host = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com").rstrip("/")
    if not api_key or _running_under_pytest() or telemetry_muted():
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


#: `%(asctime)s` pattern for every level. The trailing `Z` is a literal — `strftime` passes an
#: unrecognised character through — and it is not decoration: it is the only thing on the line that
#: says which clock read it, and the containers run `America/New_York`.
_UTC_DATEFMT = "%Y-%m-%d %H:%M:%SZ"


class _LevelFormatter(logging.Formatter):
    """Per-level line shape, timestamped in UTC.

    Issue #1839: only `_fmt_debug` carried `%(asctime)s`, and production runs at INFO, so every line
    in `/opt/lem/logs/cqc_lem_*.log` was untimestamped by construction. The only ordering an incident
    could recover was line number, which is not a clock: it cannot be compared against a deploy, a
    `docker` event or a PostHog `$exception`, and it does not even mean occurrence order, because six
    worker containers append to the one file and interleave by arrival.

    The stamp is UTC — `converter` is overridden because the stdlib default is `time.localtime` — so
    the records agree with the `_utc_today()` filename that contains them. A naive local stamp would
    be worse than none: 20:00-24:00 EDT records land in tomorrow's file wearing yesterday's hours.

    The level prefixes are unchanged. `WARNING [file:line]:` is grepped by hand and named by the
    escalation contract in `utilities/CLAUDE.md`; this adds a field, it does not restructure the
    line. DEBUG keeps its bracketed stamp rather than gaining a second one.
    """

    converter = time.gmtime

    _fmt_debug = "[%(asctime)s %(filename)s->%(funcName)s():%(lineno)s] DEBUG: %(message)s"
    _fmt_info = "%(asctime)s %(message)s"
    _fmt_warning = "%(asctime)s WARNING [%(filename)s:%(lineno)s]: %(message)s"
    _fmt_error = "%(asctime)s ERROR [%(filename)s->%(funcName)s():%(lineno)s]: %(message)s"
    _fmt_critical = "%(asctime)s CRITICAL [%(filename)s->%(funcName)s():%(lineno)s]: %(message)s"
    _fmt_fallback = "%(asctime)s %(levelname)s: %(message)s"

    _map = {
        logging.DEBUG: _fmt_debug,
        logging.INFO: _fmt_info,
        logging.WARNING: _fmt_warning,
        logging.ERROR: _fmt_error,
        logging.CRITICAL: _fmt_critical,
    }

    def __init__(self, datefmt: Optional[str] = None) -> None:
        """Build the formatter with the UTC date format baked in.

        Args:
            datefmt: `strftime` pattern for `%(asctime)s`. Defaults to `_UTC_DATEFMT`, so a caller
                that constructs this with no arguments — as the handlers below do — still gets a
                zone-marked UTC stamp instead of the stdlib's local `asctime`.
        """
        super().__init__(datefmt=datefmt or _UTC_DATEFMT)

    def format(self, record: logging.LogRecord) -> str:
        """Render `record` in its level's shape.

        Args:
            record: The record to format.

        Returns:
            The formatted line, leading with a UTC timestamp at every level except DEBUG, whose
            stamp sits inside its existing bracket.
        """
        self._style._fmt = self._map.get(record.levelno, self._fmt_fallback)
        return super().format(record)


# ── Build logger ─────────────────────────────────────────────────────────────

logger = logging.getLogger("cqc-lem")
logger.setLevel(_LOG_LEVEL)
logger.propagate = False  # don't double-log via root

_formatter = _LevelFormatter()

_file_handler = DatedRotatingFileHandler(max_bytes=_LOG_FILE_MAX_BYTES,
                                         backup_count=_LOG_FILE_BACKUP_COUNT)
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

# Recurrence escalation (see log_escalation.py). Imported defensively: a partial deploy that lacks
# the module must degrade to the pre-escalation behaviour, not break every log call in the app.
try:
    from cqc_lem.utilities import log_escalation
    _ESCALATION_AVAILABLE = True
except Exception:  # pragma: no cover - import guard
    log_escalation = None  # type: ignore[assignment]
    _ESCALATION_AVAILABLE = False


def _caller_origin() -> str:
    """`module.function` of whoever called the public log helper — part of the escalation key so a
    generic message emitted from two places stays two problems.
    """
    try:
        frame = sys._getframe(2)
        return f"{frame.f_globals.get('__name__', '?')}.{frame.f_code.co_name}"
    except Exception:
        return "?"


def _capture(exc: Optional[BaseException], message: str, level: str, context: dict,
             fingerprint: Optional[str] = None) -> None:
    """Forward a logged exception to PostHog Error Tracking. Imported lazily because
    observability.py imports this module — and swallowing everything (including a caller's context
    key colliding with a named argument), since a telemetry failure must never turn a logged error
    into a raised one.
    """
    if exc is None or not _CAPTURE_EXCEPTIONS:
        return
    try:
        from cqc_lem.utilities.observability import capture_exception
        props = dict(context)
        props["log_message"] = message
        props["log_level"] = level
        if fingerprint:
            props["fingerprint"] = fingerprint
        capture_exception(exc, **props)
    except Exception:
        pass  # lgtm[py/empty-except] Best-effort: avoid recursion if the observability backend is down.


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
    exception's stack trace (via exc_info) instead of passing it as a raw attribute.
    """
    escalation = None
    if _ESCALATION_AVAILABLE:
        # note() swallows its own errors, but this is the logging path: if escalation ever raises,
        # every warning in the app would raise with it. Belt and braces.
        try:
            escalation = log_escalation.note(message, "WARNING", _caller_origin(), context, exc=exc)
        except Exception:  # pragma: no cover - defensive
            escalation = None

    if escalation is None:
        # Fail-open path: byte-identical to the pre-escalation behaviour.
        if exc is not None:
            logger.warning(message, exc_info=exc, extra=_extra(**context))
        else:
            logger.warning(message, extra=_extra(**context))
        return

    # Crossed the threshold: emit at ERROR so it reaches PostHog Logs even at POSTHOG_LOG_LEVEL=ERROR
    # and lands in the `logs` table as severity `error`, then file it as a grouped $exception.
    escalated = _extra(**context, escalated_from="WARNING",
                       occurrence_count=escalation["count"],
                       log_fingerprint=escalation["fingerprint"])
    if exc is not None:
        logger.error(message, exc_info=exc, extra=escalated)
    else:
        logger.error(message, extra=escalated)
    log_escalation.escalate(escalation, context, exc=exc)


def log_error(
    message: str,
    exc: Optional[BaseException] = None,
    **context,
) -> None:
    """Log at ERROR level. Pass exc= to capture exception info and stack trace, and to file the
    exception as a grouped PostHog error-tracking issue.
    """
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
    exception as a grouped PostHog error-tracking issue.
    """
    if exc is not None:
        logger.critical(message, exc_info=exc, extra=_extra(**context))
        _capture(exc, message, "CRITICAL", _extra(**context))
    else:
        logger.critical(message, extra=_extra(**context))
