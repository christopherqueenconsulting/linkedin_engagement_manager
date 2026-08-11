"""Lane routing driven by REAL subscription usage.

v1 could not do this. `lib/capacity.sh` states the assumption in its own header — that no API
exposes "remaining this week" — and so it estimates capacity from rolling run outcomes and only
leaves the Claude lane AFTER a run has already been refused. That is reactive by construction: you
discover the ceiling by hitting it, and the four discrete values it produces (80/40/25/0) describe
failure history, not consumption.

The assumption turned out to be wrong. `claude -p "/usage"` answers headlessly:

    Current session: 19% used · resets Aug 10, 4:39am (UTC)
    Current week (all models): 65% used · resets Aug 13, 6:59pm (UTC)
    Current week (Fable): 30% used · resets Aug 13, 6:59pm (UTC)

That is the signal the "switch to Ollama at 50% of the week" rule always wanted: a real percentage
with a real reset time, letting the pipeline conserve BEFORE anything is refused.

Two honest limitations, both handled rather than hidden:

* The CLI labels this "approximate, based on local sessions on this machine — does not include
  other devices or claude.ai". This box is the dominant consumer, so it is a good signal, but it
  is a floor rather than a total.
* The probe costs a (small) run of its own, so it is cached and polled on a slow interval rather
  than consulted per dispatch.

There WAS a second signal here — a self-measured cost ledger fed from each run's
`--output-format json`, with `state()` and `choose_lane()` on top of it. All of it is gone (#1395),
and the reason is worth recording: it had no producer and no plausible one. `lib/run_lane.sh` emits
`estimated_cost: 0` by design, because the Claude lane is a flat-rate subscription — so the fallback
could only ever read $0, call it "normal", and route as if nothing had been spent. A second signal
that always agrees is not redundancy, it is a comment that runs.

What remains is the probe and its cache, which `daemon.refresh_usage()` fills and `lane_for.py`
reads. Routing decisions live in `lane.decide()`.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger("lemd.spend")

#: Lanes that keep Claude when usage is constrained. `selfreview` is here because it produces the
#: merge gate's review evidence — starving it does not slow merges, it STOPS them, which is the
#: livelock the v2 review flagged (H4). `start` is here because a fresh implementation is where
#: model quality shows up most.

#: How long a usage reading stays trustworthy. The probe costs a small run, so it is not per-dispatch.
USAGE_TTL = 900

_PCT = re.compile(
    r"current\s+(session|week)\s*(?:\(([^)]*)\))?\s*:\s*(\d+)%\s*used"
    r"(?:.*?resets\s+([^\n·]+))?",
    re.I,
)


@dataclass(frozen=True)
class Usage:
    """A parsed `/usage` reading."""

    session_pct: float | None = None
    week_pct: float | None = None
    week_model_pct: float | None = None
    session_resets: str = ""
    week_resets: str = ""
    at: float = 0.0
    readable: bool = False
    raw: str = ""

    @property
    def worst_pct(self) -> float | None:
        """The binding constraint: whichever window is closest to its ceiling."""
        vals = [v for v in (self.session_pct, self.week_pct) if v is not None]
        return max(vals) if vals else None


def parse_usage(text: str, *, now: float | None = None) -> Usage:
    """Parse the `/usage` output.

    Tolerant by design: the wording is a product surface that can change, so anything unparseable
    yields `readable=False` and the caller falls back to the cost ledger rather than treating a
    format change as "0% used", which would spend the whole window.
    """
    now = float(now if now is not None else time.time())
    session = week = week_model = None
    s_reset = w_reset = ""
    per_model: list[tuple[float, str]] = []
    for scope, qualifier, pct, resets in _PCT.findall(text or ""):
        value = float(pct)
        resets = (resets or "").strip()
        if scope.lower() == "session":
            session, s_reset = value, resets
        elif "all models" in (qualifier or "").lower() or not qualifier:
            week, w_reset = value, resets
        else:
            # A per-model line (e.g. "(Fable)") — tracked, but never the binding constraint while
            # an all-models line exists, since that number is what actually gates the subscription.
            per_model.append((value, resets))
    if per_model:
        week_model = max(v for v, _ in per_model)
    if week is None and per_model:
        # The all-models line went missing — a qualifier rename ("(all models)" -> "(all)") is
        # exactly the wording change this module fears. Reading only the session window here would
        # leave `readable=True`, suppress the ledger fallback, and silently ignore the week the
        # owner's rule is actually about. So fall back to the WORST per-model line: it is a floor
        # on the week, and a floor is the safe direction for a spend gate.
        week, w_reset = max(per_model, key=lambda item: item[0])
    readable = session is not None or week is not None
    return Usage(session_pct=session, week_pct=week, week_model_pct=week_model,
                 session_resets=s_reset, week_resets=w_reset, at=now,
                 readable=readable, raw=(text or "")[:500])


def probe_usage(*, timeout: int = 90) -> Usage:
    """Ask the CLI for real subscription usage.

    Runs with the Claude lane's own credential (no `ANTHROPIC_BASE_URL` override), because the
    question is "how much of the SUBSCRIPTION is left" — pointing it at LiteLLM would answer about
    the wrong provider entirely.
    """
    env_clean = {"ANTHROPIC_BASE_URL": "", "ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": ""}
    import os

    env = {k: v for k, v in os.environ.items() if k not in env_clean}
    try:
        proc = subprocess.run(
            ["claude", "-p", "/usage", "--output-format", "json",
             "--dangerously-skip-permissions"],
            capture_output=True, text=True, timeout=timeout, env=env, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        LOG.warning("usage probe failed: %s", exc)
        return Usage(readable=False)
    if proc.returncode != 0:
        LOG.warning("usage probe rc=%s: %s", proc.returncode, proc.stderr[:160])
        return Usage(readable=False)
    try:
        payload = json.loads(proc.stdout or "{}")
        text = payload.get("result") or ""
    except ValueError:
        text = proc.stdout
    return parse_usage(text)


def cached_usage(cache: str | Path, *, ttl: int = USAGE_TTL,
                 now: float | None = None, probe=probe_usage) -> Usage:
    """Return a fresh reading, probing only when the cache has aged out.

    The probe costs a real (small) run, so this is what keeps the meter from turning a cost control
    into a cost.
    """
    now = float(now if now is not None else time.time())
    p = Path(cache)
    if p.is_file():
        try:
            data = json.loads(p.read_text())
            # `isinstance`, not a bare `.get`: valid JSON that is not an object (a truncated write
            # leaving `null`, a hand-edited file) raises AttributeError, which is NOT in the caught
            # set — and a corrupt cache is a reason to re-probe, not to crash the scheduler.
            if isinstance(data, dict) and now - float(data.get("at") or 0) < ttl:
                return Usage(**{k: v for k, v in data.items() if k in Usage.__annotations__})
        except (OSError, ValueError, TypeError):
            pass  # a corrupt cache is a reason to re-probe, not to fail
    fresh = probe()
    if fresh.readable:
        # Stamp with the CALLER's clock, not the probe's. `parse_usage` timestamps with real time,
        # so trusting it made freshness untestable and — worse — meant a cache written during a
        # clock skew could look fresh for the wrong duration.
        fresh = Usage(**{**fresh.__dict__, "at": now})
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            # Atomic, like `capacity.heartbeat`: v2 dispatches concurrently, so another scheduler
            # pass can read this file mid-write. A torn read only costs a re-probe — but a re-probe
            # is a real subscription run, which is the exact thing this cache exists to avoid.
            tmp = p.with_name(p.name + ".new")
            tmp.write_text(json.dumps(fresh.__dict__))
            tmp.replace(p)
        except OSError as exc:
            LOG.warning("could not cache usage: %s", exc)
    return fresh
