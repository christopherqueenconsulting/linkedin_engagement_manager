"""Concurrency caps for the scheduler — v1's CAP arithmetic, kept in step deliberately.

This mirrors `tick.sh`'s formula rather than importing it (bash), so the pair must be changed
together; the migration's shadow phase compares the two, which is what would catch a drift.

The v2 change is not the numbers, it is what they bound. v1 gave a whole tick to ONE unit of work,
so `MAX_AGENTS` capped *overlap* and never *arrival rate*: the real ceiling was one dispatch per
5-minute cron fire (288/day), and slot 2+ was used on 39 of 2,456 recorded ticks. Here the caps
bound genuinely concurrent work, and cheap gh-only mutations get their own pool so a merge-enable
never waits behind a 20-minute implementation run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Caps:
    """Resolved concurrency limits for one scheduler pass."""

    agents: int
    gh: int
    busy_window: bool
    degraded: bool
    reason: str


def _in_busy_window(hours: str, tz: str, days: str, at: datetime | None = None) -> bool:
    """True when the clock is inside the owner's interactive window.

    Read in the configured ZONE, never a fixed UTC offset: a window written as local wall-clock
    hours must stay on those hours through a DST change, or it silently covers the wrong ones for
    half the year.
    """
    if not hours or "-" not in hours:
        return False
    try:
        start_s, end_s = hours.split("-", 1)
        start, end = int(start_s), int(end_s)
    except ValueError:
        return False
    try:
        now = at or datetime.now(ZoneInfo(tz or "UTC"))
    except Exception:
        now = at or datetime.now(ZoneInfo("UTC"))
    hour = now.hour
    in_hours = (start <= hour < end) if start <= end else (hour >= start or hour < end)
    if not in_hours:
        return False
    if days and "-" in days:
        try:
            d_start, d_end = (int(x) for x in days.split("-", 1))
        except ValueError:
            return True
        dow = now.isoweekday()
        if not (d_start <= dow <= d_end):
            return False
    return True


def compute(
    *,
    ready_count: int,
    max_agents: int,
    scale_per_issues: int,
    gh_slots: int,
    busy_hours: str = "",
    busy_tz: str = "UTC",
    busy_days: str = "",
    busy_max_agents: int = 2,
    claude_pct: int = 80,
    ollama_pct: int = 75,
    threshold: int = 50,
    at: datetime | None = None,
) -> Caps:
    """Resolve how much work may run concurrently right now.

    Args:
        ready_count: Open `agent:ready` issues; scales the cap with the backlog.
        max_agents: Hard ceiling on concurrent agent runs.
        scale_per_issues: Grant one more agent slot per this many ready issues.
        gh_slots: Size of the separate pool for cheap gh-only mutations.
        busy_hours: `"10-17"`-style window in which the owner wants the box back.
        busy_tz: IANA zone the window is expressed in.
        busy_days: `"1-5"`-style ISO weekday range the window applies to.
        busy_max_agents: Agent cap while inside the busy window.
        claude_pct: Claude lane health estimate; `<= threshold` counts as constrained.
        ollama_pct: Ollama lane health estimate.
        threshold: The ">50%" rule's boundary.
        at: Clock override for tests.

    Returns:
        Caps with `degraded` set when BOTH lanes are constrained — in which case new issue starts
        are held (the scheduler's job) and concurrency drops to 1, matching v1.
    """
    scale = max(1, scale_per_issues)
    agents = min(max_agents, 1 + ready_count // scale)
    busy = _in_busy_window(busy_hours, busy_tz, busy_days, at=at)
    reason = "backlog"
    if busy:
        agents = min(agents, busy_max_agents)
        reason = "busy_window"
    degraded = claude_pct <= threshold and ollama_pct <= threshold
    if degraded:
        agents = 1
        reason = "degraded"
    return Caps(agents=max(1, agents), gh=max(1, gh_slots), busy_window=busy,
                degraded=degraded, reason=reason)


def heartbeat(path, now: float | None = None) -> None:
    """Stamp the freshness beacon the failsafe cron reads.

    v1's `--failsafe` tick no-ops while this file is fresh and takes over when it goes stale, so a
    wedged daemon degrades to v1's cadence instead of stalling the pipeline. Written atomically:
    the cron may read it at any instant, and a truncated file must never read as "stale".
    """
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".new")
    tmp.write_text(str(int(now if now is not None else time.time())))
    tmp.replace(p)
