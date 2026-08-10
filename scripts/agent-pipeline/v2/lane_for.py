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
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lemd import lane, spend  # noqa: E402

#: How stale the daemon's reading may be before it stops counting. Generous relative to the 15-minute
#: refresh, because a slightly old percentage is a far better input than no input at all.
MAX_AGE = 3600


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
        pass

    # How long the Claude lane has been paused, for selfreview's bounded wait. The pause file holds
    # the UNIX time the pause ends, so the elapsed part is what the bound is measured against.
    paused_seconds = 0.0
    try:
        until = float(Path(os.environ.get("CLAUDE_PAUSED_FILE",
                                          str(base / "CLAUDE_PAUSED_UNTIL"))).read_text().strip())
        minutes = int(os.environ.get("USAGE_PAUSE_MINUTES", "60"))
        started = until - minutes * 60
        if until > time.time():
            paused_seconds = max(0.0, time.time() - started)
    except (OSError, ValueError):
        pass

    choice = lane.decide(
        mode, usage, paused_seconds=paused_seconds,
        usage_pause_minutes=int(os.environ.get("USAGE_PAUSE_MINUTES", "60")),
    )
    sys.stdout.write(lane.emit_env(choice) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
