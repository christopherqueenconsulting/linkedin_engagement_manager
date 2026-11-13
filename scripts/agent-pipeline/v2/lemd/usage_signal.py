"""Live provider quota, polled from the CLIProxyAPI management API and read back from a cache.

Two halves, deliberately separate, joined only by a small JSON file:

* **The poller** (`poll_once`) runs in the daemon on its own timer. It asks the proxy's read-only
  `auth-files` listing for each account's quota, normalizes the two providers the router cares about
  (Claude weekly utilization; Codex 5h and weekly utilization) and writes `state/usage_signal.json`
  atomically. It costs no model run, and it sees the whole account across every client, which the
  local `claude -p /usage` probe (`spend.py`) cannot.
* **The accessor** (`read`) is the ONLY thing later router phases call. It never touches the network
  and never guesses: a missing, stale or malformed cache is `readable=False` with every number
  `None`, so a caller cannot mistake "the meter is down" for "0% used".

Secrets: the management key comes from the process environment (`CLIPROXY_MANAGEMENT_KEY`) and the
proxy origin from `CLIPROXY_BASE_URL`. Neither is written to the cache or to a log line — failures
log the exception CLASS and HTTP status only, because a `urllib` message can carry the URL.

The draining queue endpoint on that proxy is never requested here or anywhere else: each read of it
CONSUMES records, so only a daemon that owns them may read it. `test_agent_pipeline_usage_signal.py`
fails the build if any module in this tree names it.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

LOG = logging.getLogger("lemd.usage_signal")

#: The one management path this module requests.
AUTH_FILES_PATH = "/v0/management/auth-files"
KEY_VAR = "CLIPROXY_MANAGEMENT_KEY"
BASE_VAR = "CLIPROXY_BASE_URL"

#: Cache age past which a reading is no longer trusted (about three poll intervals at the default).
DEFAULT_MAX_AGE = 300.0

# Key aliases tolerated per field. Matching ignores case, `_` and `-`, and searches the whole entry.
_PROVIDER_KEYS = ("provider", "type")
_STATUS_KEYS = ("status", "quotastatus")
_RESET_KEYS = ("resetsat", "resetat", "reset", "resettime", "weeklyreset")
_CLAUDE_WEEKLY_KEYS = ("weeklyutilization", "sevendayutilization", "utilization")
_CODEX_5H_KEYS = ("fivehourusedpercent", "primaryusedpercent", "fivehourutilization")
_CODEX_WEEKLY_KEYS = ("weeklyusedpercent", "secondaryusedpercent", "weeklyutilization")


@dataclass(frozen=True)
class Signal:
    """What the accessor returns. Numbers are percentages 0-100, or None when not readable."""

    readable: bool
    reason: str                       # "ok" | "missing" | "malformed" | "stale"
    age_s: float | None               # None when there is no fetch timestamp to age
    claude_weekly_pct: float | None = None
    claude_status: str | None = None
    claude_reset_at: float | None = None
    codex_5h_pct: float | None = None
    codex_weekly_pct: float | None = None
    codex_reset_at: float | None = None

    @property
    def unreadable(self) -> bool:
        """True when no number may be used (the deterministic default for every non-ok reason)."""
        return not self.readable


UNREADABLE = Signal(readable=False, reason="missing", age_s=None)


def _norm(key: Any) -> str:
    """Normalize a key for alias matching."""
    return str(key).lower().replace("_", "").replace("-", "")


def _find(obj: Any, aliases: tuple[str, ...]) -> Any:
    """First scalar value under any key matching an alias, searching dicts and lists depth-first."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if _norm(key) in aliases and not isinstance(value, (dict, list)):
                return value
        for value in obj.values():
            found = _find(value, aliases)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find(value, aliases)
            if found is not None:
                return found
    return None


def _num(value: Any) -> float | None:
    """A finite non-bool number, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        out = float(value)
    except ValueError:
        return None
    return out if math.isfinite(out) else None


def _pct_from_fraction(value: Any) -> float | None:
    """Claude reports utilization as a 0-1 fraction; None unless it is one."""
    n = _num(value)
    return round(n * 100, 2) if n is not None and 0.0 <= n <= 1.0 else None


def _pct(value: Any) -> float | None:
    """Codex reports used percent as 0-100; None outside that range."""
    n = _num(value)
    return round(n, 2) if n is not None and 0.0 <= n <= 100.0 else None


def _epoch(value: Any) -> float | None:
    """A reset time as epoch seconds (a bare number, or an ISO-8601 string)."""
    n = _num(value)
    if n is not None:
        return n if n > 0 else None
    if isinstance(value, str):
        from datetime import datetime, timezone

        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    return None


def _entries(payload: Any) -> list[dict]:
    """The per-account entries of an `auth-files` response, whichever envelope it uses."""
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)]
    if isinstance(payload, dict):
        for key in ("files", "auth_files", "auth-files", "data"):
            if isinstance(payload.get(key), list):
                return [e for e in payload[key] if isinstance(e, dict)]
    return []


def normalize(payload: Any, *, now: float) -> dict[str, Any] | None:
    """Reduce an `auth-files` response to the small cache document, or None when it holds no quota.

    Only the numbers, status and reset time survive: no account name, file name, email or host is
    copied across, so the cache cannot leak what the response carried.
    """
    claude: dict[str, Any] = {}
    codex: dict[str, Any] = {}
    for entry in _entries(payload):
        provider = _norm(_find(entry, tuple(_PROVIDER_KEYS)) or "")
        if provider == "claude" and not claude:
            weekly = _pct_from_fraction(_find(entry, _CLAUDE_WEEKLY_KEYS))
            if weekly is not None:
                status = _find(entry, _STATUS_KEYS)
                claude = {"weekly_pct": weekly,
                          "status": str(status) if status is not None else None,
                          "reset_at": _epoch(_find(entry, _RESET_KEYS))}
        elif provider == "codex" and not codex:
            five_h = _pct(_find(entry, _CODEX_5H_KEYS))
            weekly = _pct(_find(entry, _CODEX_WEEKLY_KEYS))
            if five_h is not None or weekly is not None:
                codex = {"five_hour_pct": five_h, "weekly_pct": weekly,
                         "reset_at": _epoch(_find(entry, _RESET_KEYS))}
    if not claude and not codex:
        return None
    return {"fetched_at": float(now), "claude": claude, "codex": codex}


def _endpoint() -> tuple[str, str] | None:
    """(url, key) from the environment, or None when either is unset."""
    key = os.environ.get(KEY_VAR, "").strip()
    parts = urlsplit(os.environ.get(BASE_VAR, "").strip())
    if not key or not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}{AUTH_FILES_PATH}", key


def fetch(*, timeout: float = 10.0, opener=urllib.request.urlopen) -> Any | None:
    """GET the `auth-files` listing. None on any failure, logged without the URL or the key."""
    endpoint = _endpoint()
    if endpoint is None:
        LOG.debug("quota poll skipped: %s or %s is not set", KEY_VAR, BASE_VAR)
        return None
    url, key = endpoint
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"}, method="GET")
    try:
        with opener(req, timeout=timeout) as resp:  # noqa: S310 - fixed https management path
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        LOG.warning("quota poll failed: HTTP %s", exc.code)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        LOG.warning("quota poll failed: %s", type(exc).__name__)
    return None


def write_cache(cache: str | Path, doc: dict[str, Any]) -> None:
    """Atomically replace the cache, so a reader never sees a torn write."""
    p = Path(cache)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".new")
    tmp.write_text(json.dumps(doc))
    tmp.replace(p)


def poll_once(cache: str | Path, *, now: float | None = None, fetcher=fetch) -> bool:
    """Fetch, normalize and cache once. True when a fresh cache was written.

    A failed or empty poll leaves the previous cache untouched, so it ages into `stale` rather than
    being overwritten with a guess.
    """
    now = float(now if now is not None else time.time())
    payload = fetcher()
    if payload is None:
        return False
    doc = normalize(payload, now=now)
    if doc is None:
        LOG.warning("quota poll: response held no Claude or Codex quota")
        return False
    try:
        write_cache(cache, doc)
    except OSError as exc:
        LOG.warning("could not cache provider quota: %s", type(exc).__name__)
        return False
    return True


def read(cache: str | Path, *, now: float | None = None, max_age: float = DEFAULT_MAX_AGE) -> Signal:
    """The accessor: cached quota with its age, or an unreadable default. Never raises."""
    now = float(now if now is not None else time.time())
    p = Path(cache)
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError:
        return Signal(readable=False, reason="missing", age_s=None)
    except (OSError, ValueError):
        return Signal(readable=False, reason="malformed", age_s=None)
    if not isinstance(data, dict):
        return Signal(readable=False, reason="malformed", age_s=None)
    fetched = _num(data.get("fetched_at"))
    claude, codex = data.get("claude"), data.get("codex")
    if fetched is None or not isinstance(claude, dict) or not isinstance(codex, dict):
        return Signal(readable=False, reason="malformed", age_s=None)
    age = now - fetched
    if age < 0 or age > max_age:
        return Signal(readable=False, reason="stale", age_s=max(age, 0.0))
    status = claude.get("status")
    return Signal(
        readable=True, reason="ok", age_s=age,
        claude_weekly_pct=_pct(claude.get("weekly_pct")),
        claude_status=status if isinstance(status, str) else None,
        claude_reset_at=_epoch(claude.get("reset_at")),
        codex_5h_pct=_pct(codex.get("five_hour_pct")),
        codex_weekly_pct=_pct(codex.get("weekly_pct")),
        codex_reset_at=_epoch(codex.get("reset_at")),
    )
