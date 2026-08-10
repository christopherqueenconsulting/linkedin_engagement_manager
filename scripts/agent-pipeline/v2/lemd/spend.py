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

When the probe cannot be read, the meter falls back to a self-measured cost ledger (`record()`
below, fed from each run's `--output-format json`). Two independent signals, because a routing rule
that silently defaults to "spend freely" is how you exhaust a window at 3am.
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
CLAUDE_PRIORITY_MODES = frozenset({"selfreview", "start"})

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


@dataclass(frozen=True)
class SpendState:
    """What the subscription has left, and what that means for routing."""

    level: str          # "normal" | "conserve" | "exhausted"
    reason: str
    usage: Usage
    source: str         # "usage_probe" | "cost_ledger" | "unknown"

    @property
    def constrained(self) -> bool:
        """True once Claude should be reserved rather than spent freely."""
        return self.level != "normal"


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


def record(ledger: str | Path, *, mode: str, lane: str, model: str | None,
           result: dict | None, run_seconds: float, rc: int, now: float | None = None) -> None:
    """Append one run's measured cost — the fallback signal when the probe is unreadable.

    A run that produced no JSON (killed, timed out, crashed before emitting) is still recorded with
    `cost_usd: 0.0` and `cost_known: false`: the meter must know a run HAPPENED even when it cannot
    know what it cost, or an expensively-failing lane would look free.
    """
    now = float(now if now is not None else time.time())
    usage = (result or {}).get("usage") or {}
    row = {
        "ts": int(now), "mode": mode, "lane": lane, "model": model or "", "rc": rc,
        "run_seconds": round(run_seconds, 1),
        # `or 0.0`, not `.get(k, 0.0)`: the key can be present and null.
        "cost_usd": float((result or {}).get("total_cost_usd") or 0.0),
        "turns": int((result or {}).get("num_turns") or 0),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cost_known": (result or {}).get("total_cost_usd") is not None,
    }
    p = Path(ledger)
    try:
        # INSIDE the try: an unwritable ledger directory raises here, and metering must never break
        # dispatch — this ran outside it, so a read-only volume would have taken down the run it
        # was only supposed to measure.
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError as exc:
        # Metering must never break dispatch; a lost row only makes the meter conservative.
        LOG.warning("could not record spend: %s", exc)


def ledger_summary(ledger: str | Path, *, since: float,
                   lane: str = "claude") -> tuple[float, int, int]:
    """Measured cost for one lane since a timestamp, and how much of it is actually KNOWN.

    Returns:
        `(priced_cost_usd, priced_runs, unpriced_runs)`. The last number is the whole point:
        `record` deliberately stores a killed run as `cost_known: false` rather than `$0`, so a
        consumer that only sums `cost_usd` re-introduces the very lie that flag exists to prevent.
    """
    p = Path(ledger)
    total, priced, unpriced = 0.0, 0, 0
    if not p.is_file():
        return total, priced, unpriced
    try:
        with p.open() as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # a torn final line must not poison the sum
                if not isinstance(row, dict):
                    continue
                if row.get("lane") != lane or float(row.get("ts") or 0) < since:
                    continue
                # Absent key means a producer older than `cost_known` — treat as priced, which is
                # what it meant before the flag existed.
                if row.get("cost_known", True):
                    total += float(row.get("cost_usd") or 0.0)
                    priced += 1
                else:
                    unpriced += 1
    except OSError as exc:
        LOG.warning("could not read spend ledger: %s", exc)
    return total, priced, unpriced


def ledger_cost(ledger: str | Path, *, since: float, lane: str = "claude") -> float:
    """Total measured cost for one lane since a timestamp."""
    return ledger_summary(ledger, since=since, lane=lane)[0]


def state(*, usage: Usage, ledger: str | Path | None = None,
          weekly_budget_usd: float = 0.0, conserve_at: float = 50.0,
          exhaust_at: float = 85.0, now: float | None = None) -> SpendState:
    """Decide the spend level, preferring real usage over the cost estimate.

    Args:
        usage: A `/usage` reading (possibly unreadable).
        ledger: Cost ledger, used only when `usage` is unreadable.
        weekly_budget_usd: Ceiling for the ledger fallback; 0 disables it.
        conserve_at: Percent at which Claude is reserved for priority lanes — the "50% rule",
            now measured instead of estimated.
        exhaust_at: Percent at which the Claude lane stops entirely.
        now: Clock override for tests.
    """
    now = float(now if now is not None else time.time())
    if usage.readable and usage.worst_pct is not None:
        pct = usage.worst_pct
        which = "session" if (usage.session_pct or 0) >= (usage.week_pct or 0) else "week"
        if pct >= exhaust_at:
            level = "exhausted"
        elif pct >= conserve_at:
            level = "conserve"
        else:
            level = "normal"
        return SpendState(level=level, reason=f"{which} at {pct:.0f}% used", usage=usage,
                          source="usage_probe")

    if ledger and weekly_budget_usd > 0:
        spent, priced, unpriced = ledger_summary(ledger, since=now - 7 * 86400)
        if priced:
            # Charge the runs that died before reporting a cost at the lane's OWN measured mean.
            # Summing `cost_usd` alone would price them at $0 — the "expensively failing lane looks
            # free" failure `record`'s `cost_known` flag was written to prevent, and nothing read.
            estimate = spent + unpriced * (spent / priced)
            pct = 100.0 * estimate / weekly_budget_usd
            level = ("exhausted" if pct >= exhaust_at
                     else "conserve" if pct >= conserve_at else "normal")
            note = f", {unpriced} unpriced run(s) at the measured mean" if unpriced else ""
            return SpendState(level=level,
                              reason=f"ledger estimate {pct:.0f}% of weekly budget{note}",
                              usage=usage, source="cost_ledger")
        if unpriced:
            # Every run in the window died before it could report a cost. That is NOT "$0 spent",
            # and there is no mean to impute from — so it is no signal, not a reassuring 0%.
            LOG.warning("spend ledger has %d run(s) but no priced one — treating as no signal",
                        unpriced)
            return SpendState(level="normal", reason=f"{unpriced} unpriced run(s), cost unknown",
                              usage=usage, source="unknown")
        pct = 0.0
        return SpendState(level="normal", reason=f"ledger estimate {pct:.0f}% of weekly budget",
                          usage=usage, source="cost_ledger")

    # Neither signal available. Do NOT silently conserve: that would route everything to Ollama
    # indefinitely on a parse regression. Log loudly and behave as normal — the usage-limit
    # pause/probe loop remains the backstop it has always been.
    LOG.warning("no usage signal available — routing as normal; check the /usage probe")
    return SpendState(level="normal", reason="no usage signal", usage=usage, source="unknown")


def choose_lane(mode: str, spend: SpendState, *, ollama_available: bool = True,
                pinned: str | None = None) -> tuple[str, str]:
    """Pick the lane for one run.

    Returns:
        `(lane, reason)`. The reason is telemetry, not decoration: it is how a surprising routing
        decision gets explained afterwards, which v1 could never do.

    The escalation is deliberate rather than one threshold:

    * `normal` — Claude, as today.
    * `conserve` — Claude RESERVED for the lanes that need it; mechanical work moves to Ollama.
      This is the state v1 could never reach, because it only reacted to a refusal.
    * `exhausted` — Ollama only, including priority lanes. A tier-3 adversarial review carrying a
      warning marker beats no merges at all.

    An unavailable Ollama lane always yields Claude: degrading quality is acceptable, halting is
    not, and a refusal is a recoverable outcome the pause/probe loop already handles.
    """
    if pinned:
        return pinned, "owner_pinned"
    if not ollama_available:
        return "claude", "ollama_unavailable"
    if spend.level == "normal":
        return "claude", "usage_normal"
    if spend.level == "conserve":
        if mode in CLAUDE_PRIORITY_MODES:
            return "claude", f"priority_mode_reserved ({spend.reason})"
        return "ollama", f"conserving_claude ({spend.reason})"
    return "ollama", f"claude_exhausted ({spend.reason})"


def parse_cli_result(stdout: str) -> dict | None:
    """Extract the result object from a `--output-format json` run.

    The last parseable object wins, since a wrapper may prepend noise. Returns None when nothing
    parses — which `record` treats as "cost unknown", never as "cost zero".
    """
    best = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and ("total_cost_usd" in obj or "usage" in obj):
            best = obj
    return best
