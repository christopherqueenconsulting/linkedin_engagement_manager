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
    half the year. An `at` override is CONVERTED into that zone rather than read as-is — a caller
    holding a UTC clock (the natural thing for a scheduler) would otherwise compare UTC hours
    against local window bounds and be wrong by the whole offset.
    """
    if not hours or "-" not in hours:
        return False
    try:
        start_s, end_s = hours.split("-", 1)
        start, end = int(start_s), int(end_s)
    except ValueError:
        return False
    try:
        zone = ZoneInfo(tz or "UTC")
    except Exception:
        zone = ZoneInfo("UTC")
    if at is None:
        now = datetime.now(zone)
    elif at.tzinfo is None:
        # Naive means "already local to the window's zone" — the only reading that isn't a guess.
        now = at.replace(tzinfo=zone)
    else:
        now = at.astimezone(zone)
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
        at: Clock override for tests.

    Returns:
        Caps for this pass. `degraded` is always False — see below.

    The `degraded` arm is gone. It dropped concurrency to 1 when BOTH lanes were constrained, read
    off `claude_pct`/`ollama_pct` — percentages nothing in v2 can produce, because they were
    `lib/capacity.sh`'s bash failure-history estimate and v2 never calls it. The two questions it
    was answering both have owners now: `lane.decide()` owns "which lane", and `LEMD_HOLD_STARTS`
    owns "hold new work". An arm no caller can trip is how a safety control rots into decoration.
    """
    scale = max(1, scale_per_issues)
    agents = min(max_agents, 1 + ready_count // scale)
    busy = _in_busy_window(busy_hours, busy_tz, busy_days, at=at)
    reason = "backlog"
    if busy:
        agents = min(agents, busy_max_agents)
        reason = "busy_window"
    return Caps(agents=max(1, agents), gh=max(1, gh_slots), busy_window=busy,
                degraded=False, reason=reason)


def heartbeat(path, now: float | None = None) -> None:
    """Stamp the freshness beacon the failsafe cron reads.

    v1's `--failsafe` tick no-ops while this file is fresh and takes over when it goes stale, so a
    wedged daemon degrades to v1's cadence instead of stalling the pipeline. Written atomically:
    the cron may read it at any instant, and a truncated file must never read as "stale".
    """
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".new")  # with_suffix would REPLACE .heartbeat, not append
    tmp.write_text(str(int(now if now is not None else time.time())))
    tmp.replace(p)
