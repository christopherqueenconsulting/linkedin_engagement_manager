#!/usr/bin/env python3
"""Print the lane decision for one mode as shell assignments. Called by `actions/agent_run.sh`.

    eval "$(v2/lane_for.py selfreview)"

READ-ONLY on the usage meter by design: it consults the cache the daemon refreshes and never probes.
A probe costs a real `claude -p` run, so probing here would spend a subscription run on every
dispatch in order to decide how to spend subscription runs.

A stale or missing cache prints only `LEM_LANE_REASON='probe_unavailable'`, no override — the
caller keeps `lib/dispatch.sh`'s health-based routing. That is the one honest fail-safe: preferring
Claude would burn a window nobody measured, and preferring Ollama is the exact failure this exists
to fix.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lemd import lane, route, spend, usage_signal  # noqa: E402

#: How stale the daemon's reading may be before it stops counting. Generous relative to the 15-minute
#: refresh, because a slightly old percentage is a far better input than no input at all.
MAX_AGE = 3600


def _pause_minutes() -> int:
    """`USAGE_PAUSE_MINUTES`, defaulting when unset, EMPTY, or not a number.

    Empty matters: the caller forwards this out of `config.env`, and `int("")` raises. A crash here
    would take the whole lane decision down and leave the run unrouted, so an unreadable value falls
    back to the documented default instead.
    """
    raw = (os.environ.get("USAGE_PAUSE_MINUTES") or "").strip()
    try:
        return int(raw)
    except ValueError:
        return 60


def main(argv: list[str]) -> int:
    """Resolve and print the decision."""
    mode = argv[1] if len(argv) > 1 else ""
    base = Path(os.environ.get("BASE", "/home/lem/agent-pipeline"))
    cache = base / "state" / "usage.json"

    usage = spend.Usage(readable=False)
    try:
        data = json.loads(cache.read_text())
        if isinstance(data, dict) and time.time() - float(data.get("at") or 0) < MAX_AGE:
            usage = spend.Usage(**{k: v for k, v in data.items() if k in spend.Usage.__annotations__})
    except (OSError, ValueError, TypeError):
        # Absent, unreadable, truncated mid-write, or hand-edited: every one of those means the same
        # thing here — there is no trustworthy reading. `usage` stays `readable=False`, which makes
        # `lane.decide` return `probe_unavailable` and leaves the existing health-based routing in
        # place. Raising instead would take down a dispatch over a stale cache file, and defaulting
        # to a lane would be a routing decision made from no evidence.
        pass

    # How long the Claude lane has been paused, for selfreview's bounded wait. The pause file holds
    # the UNIX time the pause ends, so the elapsed part is what the bound is measured against.
    paused_seconds = 0.0
    try:
        until = float(Path(os.environ.get("CLAUDE_PAUSED_FILE",
                                          str(base / "CLAUDE_PAUSED_UNTIL"))).read_text().strip())
        minutes = _pause_minutes()
        started = until - minutes * 60
        if until > time.time():
            paused_seconds = max(0.0, time.time() - started)
    except (OSError, ValueError):
        # No pause file, or an unparseable one. Both mean "the Claude lane is not paused as far as
        # we can tell", which leaves `paused_seconds` at 0 — and 0 is the SAFE reading: it makes
        # selfreview prefer Claude rather than believing a wait has already been exhausted and
        # downgrading the review to tier 3. The bounded wait can only be entered on evidence.
        pass

    choice = lane.decide(
        mode, usage, paused_seconds=paused_seconds,
        usage_pause_minutes=_pause_minutes(),
        router=_router_context(mode, base, paused_seconds),
    )
    sys.stdout.write(lane.emit_env(choice) + "\n")
    return 0


def _int_env(name: str, default: int = 0) -> int:
    """A non-negative int from the environment; unset, empty or junk is the default."""
    try:
        return max(int((os.environ.get(name) or "").strip()), 0)
    except ValueError:
        return default


def _issue_facts() -> tuple[frozenset[str], int, int]:
    """(labels, body characters, planned file count) for the issue, or empty when unreadable.

    `LEM_ROUTE_LABELS` / `LEM_ROUTE_BODY_CHARS` / `LEM_ROUTE_FILES` take precedence, which is how a
    test or a DRY_RUN tick supplies them without a network read. Otherwise one `gh issue view`.
    """
    labels_raw = os.environ.get("LEM_ROUTE_LABELS")
    if labels_raw is not None:
        return (route.parse_labels(labels_raw), _int_env("LEM_ROUTE_BODY_CHARS"),
                _int_env("LEM_ROUTE_FILES"))
    issue = (os.environ.get("ISSUE") or "").strip()
    if not issue.isdigit():
        return frozenset(), 0, 0
    try:
        out = subprocess.run(["gh", "issue", "view", issue, "--json", "labels,body"],
                             capture_output=True, text=True, timeout=20, check=False)
        data = json.loads(out.stdout or "{}")
        labels = frozenset(str(x.get("name", "")) for x in data.get("labels", []))
        body = str(data.get("body") or "")
    except (OSError, ValueError, subprocess.SubprocessError):
        return frozenset(), 0, 0   # unreadable facts score 0: the code default, never an escalation
    return labels, len(body), len(set(re.findall(r"`([\w./-]+\.\w+)`", body)))


def _router_context(mode: str, base: Path, paused_seconds: float) -> route.Context | None:
    """The model router's context, or None when `LEM_ROUTER` is `off` (today's routing, untouched)."""
    flag = route.router_mode()
    if flag == "off":
        return None
    labels, body_chars, files = _issue_facts()
    env = {k: v for k, v in os.environ.items() if k in route.DEFAULTS}
    attempt = max(_int_env("LEM_ROUTE_ATTEMPT", 1), 1)
    inputs = route.Inputs(
        mode=mode, labels=labels, body_chars=body_chars, planned_files=files,
        ci_failures=_int_env("LEM_ROUTE_CI_FAILURES", _int_env("ATTEMPTS") if mode == "fix" else 0),
        conflicts=_int_env("LEM_ROUTE_CONFLICTS"), attempt=attempt,
        protected_path=os.environ.get("LEM_ROUTE_PROTECTED") == "1",
    )
    signal = usage_signal.read(base / "state" / "usage_signal.json")
    return route.Context(
        flag=flag, inputs=inputs, signal=signal, env=env,
        paused=frozenset({route.CLAUDE}) if paused_seconds > 0 else frozenset(),
        bad_models=route.parse_labels(os.environ.get("LEM_ROUTE_BAD_MODELS", "")),
        proxy_down=os.environ.get("LEM_ROUTE_PROXY_DOWN") == "1",
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
