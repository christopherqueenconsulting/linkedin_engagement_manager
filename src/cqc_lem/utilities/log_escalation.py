"""Recurrence-based severity escalation: once is a warning, repeatedly is a defect.

A `log_warning` can never reach PostHog Error Tracking — `_capture()` runs only from
`log_error`/`log_critical` and only with `exc=`. So a selector that has been missing for three
straight days and a one-off transient looked identical: both a WARNING, neither an `$exception`,
neither ever filed by the daily error->issue cron. This module is what tells them apart.

It counts occurrences per (call site, normalized message) in Redis and, once a key crosses the
threshold inside the window, hands the caller an escalation record. `logger.log_warning` then emits
the record at ERROR and captures a `RecurringWarning` so the problem groups into ONE PostHog issue
and gets filed once.

Kept out of logger.py deliberately: logger.py is imported by everything (observability.py imports it
at module scope), so it has to stay dependency-light. Keeping the pure parts — normalization,
fingerprinting, threshold arithmetic — here means they are unit-testable without importing redis or
the posthog SDK.
"""
import hashlib
import os
import re
import threading
import time
from typing import Optional, Tuple


class RecurringWarning(Exception):
    """A warning that repeated often enough inside the window to be a defect rather than noise."""


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def enabled() -> bool:
    """The kill switch for the whole mechanism — ON unless `LOG_ESCALATION_ENABLED` says otherwise.

    Read per call, not at import, so a run of noisy escalations can be stopped on a live box without
    a deploy. Off means a repeated warning stays a WARNING: nothing else about logging changes.
    """
    return _bool_env("LOG_ESCALATION_ENABLED", True)


def _threshold() -> int:
    return max(1, _int_env("LOG_ESCALATE_THRESHOLD", 3))


def _window() -> int:
    return max(60, _int_env("LOG_ESCALATE_WINDOW_SECONDS", 86400))


def _repeat_every() -> int:
    return max(0, _int_env("LOG_ESCALATE_REPEAT_EVERY", 10))


def _max_per_window() -> int:
    return max(0, _int_env("LOG_ESCALATE_MAX_PER_WINDOW", 50))


def _local_cap() -> int:
    return max(1, _int_env("LOG_ESCALATE_LOCAL_CAP", 200))


# Prefixes that never escalate, whatever the environment says. `LOG_ESCALATE_EXCLUDE` ADDS to this
# tuple instead of replacing it — the first entry breaks the one real feedback loop
# (capture_exception's own failure handler calls log_warning), and an env override that dropped it
# would reintroduce that loop silently.
BUILTIN_EXCLUDED_PREFIXES = (
    "Could not capture exception in PostHog",
    # `cost_alerts.send_cost_alerts`' per-breach digest line (`cost_alerts.ALERT_LOG_PREFIX`). A
    # budget breach is a MEASUREMENT this alerter was asked to report — already delivered as a
    # `cost_alert` event and an owner email — and it recurs for as many days as the cost does, so
    # repetition carries no new information. Escalating it filed a code defect (#1071) against
    # tooling that was working exactly as designed.
    "Cost alert [",
    # `linkedin.helper._wait_for_manual_approval`'s prompt (`helper.DEVICE_APPROVAL_LOG_PREFIX`). A
    # LinkedIn device-approval challenge is a security check the account owner clears with a mobile
    # tap, not a bug — it is already best-effort handled (SPA badge, owner email) and degrades
    # gracefully into a bounded wait. Escalating it filed a GitHub issue against working login
    # handling for an event only the owner can act on (#1922).
    "LinkedIn device-approval required",
)


def _excluded_prefixes() -> tuple:
    raw = os.getenv("LOG_ESCALATE_EXCLUDE", "") or ""
    return BUILTIN_EXCLUDED_PREFIXES + tuple(p.strip() for p in raw.split(",") if p.strip())


def _is_self_clearing_backoff(exc: Optional[BaseException]) -> bool:
    """Whether `exc` is a KNOWN, self-clearing back-off that must never itself escalate.

    A `LinkedInRateLimited` is, by definition, a back-off that clears on its own — the 429 breaker,
    a manual pause, or a per-account challenge cooldown — and every raise site already downgrades it
    to WARNING for exactly that reason (`utilities/CLAUDE.md`'s "once is a warning, repeatedly is a
    defect" only holds when repetition carries NEW information).

    A per-account challenge cooldown defaults to 6h and the follow-up beat that hits it re-dispatches
    every 30min (issue #1948), so the SAME already-downgraded warning re-crosses this module's own
    threshold well before the cooldown clears — escalating the exact condition the WARNING was
    written to stop paging on. Message-prefix exclusion (see `BUILTIN_EXCLUDED_PREFIXES`) does not
    scale here: every `LinkedInRateLimited` catch across the engagement lanes writes its own message,
    so this is keyed on the EXCEPTION TYPE instead — it is the one property every one of those call
    sites shares, and a new call site is covered for free.

    Lazy import (like `_redis()` below): `rate_limit.py` imports `log_warning` from this package's
    sibling `logger.py`, so an eager import here would cycle.
    """
    if exc is None:
        return False
    try:
        from cqc_lem.utilities.linkedin.rate_limit import LinkedInRateLimited
        return isinstance(exc, LinkedInRateLimited)
    except Exception:
        return False


# ── Message normalization ─────────────────────────────────────────────────────
# The logger only ever sees the INTERPOLATED string, so volatile tokens are masked out before
# hashing. Order matters: URLs before the bare-number rule, or a URL's digits get masked first and
# the URL pattern no longer matches.
_MASKS = (
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<email>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
     "<uuid>"),
    (re.compile(r"\burn:[\w:]+\b"), "<urn>"),
    (re.compile(r"\b[0-9a-fA-F]{8,}\b"), "<hex>"),
    (re.compile(r"\[[^\]]*\]"), "<list>"),
    # `masked_recipient` depends on this quoted-value rule to collapse a contact name — keep it if
    # you reorder or tighten the table, or names start leaking into public issues.
    (re.compile(r"'[^']*'"), "<str>"),
    (re.compile(r'"[^"]*"'), "<str>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<n>"),
    (re.compile(r"\s+"), " "),
)

_MAX_MESSAGE = 300


def normalize_message(message: str) -> Tuple[str, str]:
    """-> (display, key). `display` keeps original casing and becomes the exception's value, so the
    filed issue is readable; `key` is casefolded and is what gets hashed.
    """
    text = (message or "")[:_MAX_MESSAGE]
    for pattern, replacement in _MASKS:
        text = pattern.sub(replacement, text)
    display = text.strip()
    return display, display.casefold()


def masked_recipient(first_name: Optional[str], profile_url: Optional[str]) -> str:
    """A recipient label safe to interpolate into a warning message.

    Only WARNING and above escalate (see `note`/`escalate`), so this is for those levels: a real
    contact's name must never reach the grouped `$exception` value the daily error->issue cron files
    as a public GitHub issue, and per-contact names must not fork one defect into a separate issue
    each. A known first name is quoted so `normalize_message` collapses it through the quoted-value
    mask above; the profile-URL fallback is already collapsed by the URL mask.

    Inner single quotes become a typographic apostrophe first: the ASCII quoted-value mask stops at
    the first `'`, so a name like O'Brien would otherwise leak its tail and fork the fingerprint.
    """
    name = (first_name or "").strip().replace("'", "’")
    if name:
        return f"'{name}'"
    return profile_url or "<unknown recipient>"


def fingerprint(level: str, origin: str, key_text: str) -> str:
    """Stable 12-hex id for (level, call site, normalized message).

    The call site is part of the key so a generic message emitted from two modules stays two
    problems. It is module+function and deliberately NOT the line number — a line moving during an
    unrelated edit must not reset the counter and fork a new issue.
    """
    raw = f"{level}|{origin}|{key_text}".encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:12]  # nosec B324 - grouping key, not a security boundary


# ── Redis-backed counter ──────────────────────────────────────────────────────
# redis.Redis.from_url does not connect eagerly, so a dead Redis would otherwise cost the 2s
# socket_connect_timeout on EVERY warning across ~400 call sites. After a few consecutive failures
# this process stops calling Redis at all until the cooldown passes.
_lock = threading.Lock()
_redis_circuit = {"failures": 0, "disabled_until": 0.0}
_local_counts: dict = {}
_state = threading.local()


def _redis():
    with _lock:
        if time.monotonic() < _redis_circuit["disabled_until"]:
            return None
    try:
        from cqc_lem.utilities.linkedin.rate_limit import shared_redis_client
        return shared_redis_client()
    except Exception:
        return None


def _record_redis_failure() -> None:
    with _lock:
        failures = _redis_circuit["failures"] + 1
        if failures >= _int_env("LOG_ESCALATE_REDIS_FAILURES", 3):
            _redis_circuit["disabled_until"] = time.monotonic() + _int_env(
                "LOG_ESCALATE_REDIS_COOLDOWN_SECONDS", 300)
            failures = 0
        _redis_circuit["failures"] = failures


def _record_redis_success() -> None:
    with _lock:
        _redis_circuit["failures"] = 0


def _should_escalate(count: int) -> bool:
    threshold = _threshold()
    if count == threshold:
        return True
    repeat = _repeat_every()
    # Re-escalate periodically past the threshold so the $exception's own occurrence count tracks
    # real magnitude; escalating exactly once per window would make every issue read "1x".
    return bool(repeat) and count > threshold and (count - threshold) % repeat == 0


def note(message: str, level: str, origin: str, context: Optional[dict] = None,
         exc: Optional[BaseException] = None) -> Optional[dict]:
    """Count this occurrence; return an escalation record when it crosses the threshold, else None.

    Never raises. Returning None means "carry on exactly as before" — that is the fail-open path for
    a missing Redis, a disabled switch, an excluded message, an excluded exception type, or any
    internal error.
    """
    try:
        if not enabled() or getattr(_state, "escalating", False):
            return None
        if any(message.startswith(prefix) for prefix in _excluded_prefixes()):
            return None
        if _is_self_clearing_backoff(exc):
            return None

        display, key_text = normalize_message(message)
        fp = fingerprint(level, origin, key_text)

        # A runaway loop in one process must not hammer Redis; past the local cap this fingerprint
        # stops calling out entirely for the rest of the window.
        with _lock:
            local = _local_counts.get(fp, 0) + 1
            if len(_local_counts) > 1024:
                _local_counts.clear()
            _local_counts[fp] = local
        if local > _local_cap():
            return None

        client = _redis()
        if client is None:
            return None

        window = _window()
        bucket = int(time.time()) // window
        try:
            pipe = client.pipeline(transaction=False)
            pipe.incr(f"lem:logesc:c:{fp}")
            pipe.expire(f"lem:logesc:c:{fp}", window)
            pipe.incr(f"lem:logesc:budget:{bucket}")
            pipe.expire(f"lem:logesc:budget:{bucket}", window)
            results = pipe.execute()
            _record_redis_success()
        except Exception:
            _record_redis_failure()
            return None

        count = int(results[0])
        budget = int(results[2])
        if not _should_escalate(count):
            return None
        # Ceiling across ALL fingerprints, so a bad deploy can't flood error tracking.
        if _max_per_window() and budget > _max_per_window():
            return None

        return {"count": count, "fingerprint": fp, "display": display,
                "window": window, "origin": origin, "level": level}
    except Exception:
        return None


def escalate(result: dict, context: Optional[dict] = None, exc: Optional[BaseException] = None) -> None:
    """Capture the escalated record as a grouped PostHog `$exception`. Never raises.

    Grouping is pinned with an explicit `$exception_fingerprint`. PostHog's default fingerprint is
    exception type + first in-app stack frame, and every `Selector miss: ...` is raised from the same
    line of `find_first` — so without the override, "Feed sort control" and "Open reactions menu"
    would collapse into ONE issue and the distinct breakages would be invisible.
    """
    if getattr(_state, "escalating", False):
        return
    _state.escalating = True
    try:
        from cqc_lem.utilities.observability import capture_exception
        props = dict(context or {})
        props.update({
            "escalated_from": "WARNING",
            "escalation_count": result["count"],
            "escalation_window_seconds": result["window"],
            "log_fingerprint": result["fingerprint"],
            "log_origin": result["origin"],
        })
        # Reuse the real exception when the call site had one — a genuine stack groups and debugs
        # better than a synthetic frame. Otherwise build (never raise) a RecurringWarning whose
        # message is the masked text, so the PostHog issue title reads as the problem.
        error = exc if exc is not None else RecurringWarning(result["display"])
        capture_exception(error, fingerprint=f"lem-log:{result['fingerprint']}", **props)
    except Exception:
        pass  # lgtm[py/empty-except] Telemetry must never turn a logged warning into a raised one.
    finally:
        _state.escalating = False


def reset_state() -> None:
    """Test hook — clears the process-local counters and the Redis circuit."""
    with _lock:
        _local_counts.clear()
        _redis_circuit["failures"] = 0
        _redis_circuit["disabled_until"] = 0.0
    _state.escalating = False
