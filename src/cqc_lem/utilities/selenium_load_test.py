"""Concurrency / scale load-test harness for the Selenium topology (issue #556).

`docs/scaling-plan.md` says the failure mode at scale is **"tasks miss their window"**, not "the box
falls over" (§2/§3), and Phase 2 gates a Grid + VPS decision on *"load-test the topology before
onboarding the cohort"*. This module is that test. It answers one question at a given user count:

    with THESE lane concurrencies and THIS many Chrome session slots, what fraction of each user's
    engagement work starts inside its window — and what would it cost to serve the demand?

Two modes, because both are needed and neither substitutes for the other:

* **simulate** (default, no browsers) — a discrete-event model of the real topology: per-lane Celery
  slots plus a global Chrome session cap, fed the actual per-user daily workload from §4 and the
  actual fan-out times from §3. Runs anywhere, in CI, in a second, and is the only way to get a
  number for 50/100 users *without* first buying the box that could host them. Every projection it
  prints is arithmetic on measured inputs, never a guess about how fast Chrome is.
* **live** (`--live`, opt-in) — opens N REAL concurrent sessions against the hub and measures
  session-acquisition wait, busy slots and host headroom. This is what validates the model's
  assumptions on the topology actually deployed, at whatever concurrency the box can take.

Structured like `utilities/capacity_alerts.py`: PURE functions first (what the unit tests pin), then
the live/measuring layer, then the CLI. Nothing here writes to the capacity monitor's rolling
windows — synthetic load must never be mistaken for production evidence by `auto_capacity_watch`.
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from cqc_lem.utilities.engagement_window import (
    STAGGER_APPRECIATION_DM, STAGGER_GOLDEN_HOUR, STAGGER_TICK_MINUTES, stagger_offset_minutes,
)
from cqc_lem.utilities.logger import log_info, log_warning

# ───────────────────────────── topology defaults ─────────────────────────────
# These MIRROR docker-compose.yml. tests/unit/app/test_selenium_capacity.py asserts they match, so a
# lane bump that forgets this file fails the build instead of quietly load-testing the old topology.
DEFAULT_LANES: dict[str, int] = {"se_engage": 3, "se_prepost": 2, "se_outreach": 2, "se_content": 1}
DEFAULT_SESSION_CAP = 8
# selenium/node-chrome runs one session per node (docker-compose.grid.yml), so on a Grid the node
# count IS the session cap.
GRID_NODE_SESSIONS = 1

# ───────────────────────────── resource model (scaling-plan §4/§5b) ─────────────────────────────
# A LinkedIn Selenium session peaks 0.7–1.5 GB; the midpoint is what the projections use.
MEM_PER_SESSION_GB = 1.2
CPU_PER_SESSION = 1.0
# The node JVM that wraps each Chrome on a Grid — the price of fault isolation (§5b).
GRID_NODE_OVERHEAD_GB = 0.3
GRID_HUB_CPU = 0.5
GRID_HUB_MEM_GB = 0.5
# Everything that is not Chrome, measured on the live box (§1: ~4 GB across app containers, load
# ~0.8 on 8 cores at 1 user; headroom for the app tier at N users is folded in here).
BASELINE_CPU = 1.5
BASELINE_MEM_GB = 6.0
# The current production VPS (§1).
HOST_CPUS = 8
HOST_MEM_GB = 31.0
# Verdict boundaries, calibrated to reproduce §5c's own three rows from its own numbers:
# "fits" = peak demand stays inside the cores and under 70% of RAM (10 users: 6-8 vCPU, 10-12 GB).
# "at ceiling" = the box is oversubscribed at the peak but survives it, because the peaks are brief
# and the alternative is idle hardware the rest of the day (50 users: 12-14 vCPU on 8 cores).
# "exceeds one VPS" = 2x the cores or past RAM — no amount of scheduling fixes that (100 users).
CPU_OVERSUBSCRIPTION_LIMIT = 2.0
MEM_FITS_PCT = 70.0
MEM_CEILING_PCT = 100.0

VERDICT_FITS = "fits"
VERDICT_AT_CEILING = "at ceiling"
VERDICT_EXCEEDS = "exceeds one VPS"

# ───────────────────────────── workload model (scaling-plan §3/§4) ─────────────────────────────
ARRIVAL_FANOUT = "fanout"  # a single crontab that dispatches for every active user at once
ARRIVAL_POST = "post"      # anchored to the user's own scheduled post time

# Users' posts are spread across the afternoon peak band (minutes from UTC midnight). Deterministic,
# not random: the same command must produce the same curve so two runs can be compared.
POST_BAND = (12 * 60, 16 * 60)


@dataclass(frozen=True)
class JobSpec:
    """One recurring Selenium job in a user's day.

    `tolerance_minutes` is what "on time" MEANS for that job, and it differs by an order of
    magnitude: the pre-post warm-up exists to run BEFORE a post goes live, so five minutes late is a
    miss; a golden-hour loop only has to land inside the hour.
    """
    name: str
    lane: str
    duration_minutes: float
    tolerance_minutes: float
    arrival: str = ARRIVAL_FANOUT
    # ARRIVAL_FANOUT: minutes-of-day the crontab fires, one entry per daily occurrence.
    starts: tuple[int, ...] = ()
    # ARRIVAL_POST: minutes relative to the user's post time (the eta offsets in run_scheduler.py).
    post_offset_minutes: float = 0.0
    staggerable: bool = True
    # The window this fan-out is ACTUALLY staggered across in production today (issue #554), used
    # whenever the CLI doesn't pass an explicit `--stagger-hours` override. 0 means "not one of
    # #554's three staggered fan-outs" — its arrivals stay a single fixed-time spike by default.
    stagger_window_minutes: float = 0.0


# Sums to ~68 Selenium-minutes/user/day — the §4 table this plan's math is built on. The tolerances
# are the product requirement, not a guess: they are why 100 users do not need 100 browsers.
WORKLOAD: tuple[JobSpec, ...] = (
    # eta = post − 15 min, on the dedicated se_prepost lane (#553). Its ONLY job is to warm the feed
    # before the post goes live, so a few minutes late means it did nothing — the tightest window here.
    JobSpec("prepost_commenting", "se_prepost", 15.0, 5.0,
            arrival=ARRIVAL_POST, post_offset_minutes=-15.0, staggerable=False),
    # #554 replaced the single 13:00 UTC fan-out with a `*/15 min` beat that dispatches each user at
    # a stable hashed minute inside a window anchored (in the user's OWN timezone) at 09:00 local —
    # `stagger_window_minutes` mirrors production's `GOLDEN_HOUR_WINDOW_MINUTES` default (issue #634).
    # `13 * 60` remains the model's single-timezone stand-in for that local anchor (same convention
    # POST_BAND uses), so `--stagger-hours 0` still reproduces the pre-#554 single-minute fan-out for
    # a before/after comparison. Tolerance = the 2-hour golden window §4's C ≥ N ÷ (W×4) formula assumes.
    JobSpec("golden_hour_commenting", "se_engage", 15.0, 120.0, starts=(13 * 60,),
            stagger_window_minutes=float(STAGGER_GOLDEN_HOUR[2])),
    # Dispatched every 30 min and Redis-gated to the user's cadence, so a sweep that starts within
    # the next dispatch interval is indistinguishable from one that started on its own tick.
    JobSpec("reply_sweep", "se_engage", 4.0, 30.0, starts=(11 * 60, 19 * 60)),
    # eta = post − 10 min (run_scheduler.py), so it is post-anchored, not a crontab.
    JobSpec("profile_viewer_engagement", "se_outreach", 10.0, 30.0,
            arrival=ARRIVAL_POST, post_offset_minutes=-10.0, staggerable=False),
    # §3's "fixed off-peak batch": nothing about an appreciation DM or a stats scrape breaks if it
    # runs an hour or three into the morning, so their windows are wide on purpose. Giving these the
    # same tolerance as a golden-hour loop would demand browsers the product does not actually need.
    # Also staggered by #554 (`APPRECIATION_DM_WINDOW_MINUTES`, default 120) — see the golden-hour
    # comment above for why the model keeps `8 * 60` as the anchor stand-in.
    JobSpec("appreciation_dms", "se_outreach", 10.0, 240.0, starts=(8 * 60,),
            stagger_window_minutes=float(STAGGER_APPRECIATION_DM[2])),
    # `scrape-post-stats` — a single fixed-time batch #554 did NOT touch (only golden-hour,
    # appreciation DMs and group-engagement were staggered), so it stays a single-minute fan-out
    # regardless of `--stagger-hours` (`staggerable` gates the CLI override too).
    JobSpec("content_tasks", "se_content", 10.0, 240.0, starts=(23 * 60,), staggerable=False),
)


@dataclass(frozen=True)
class Job:
    user_id: int
    name: str
    lane: str
    ready_at: float       # minutes from UTC midnight — when the task hits its queue
    duration: float
    tolerance: float


@dataclass(frozen=True)
class Topology:
    lanes: Mapping[str, int]
    session_cap: int
    label: str = "standalone"

    @property
    def requested_concurrency(self) -> int:
        return sum(self.lanes.values())

    @property
    def matches_invariant(self) -> bool:
        """The cap == Σ lanes rule from §5a. Below it lanes block on session creation; above it,
        slots are paid for and idle."""
        return self.requested_concurrency == self.session_cap


@dataclass(frozen=True)
class JobResult:
    job: Job
    started_at: float
    # When a lane slot first came free for this job. Everything after it was spent waiting on a
    # Chrome session, which is the number that says "the cap, not the lane, is the constraint".
    lane_ready_at: float

    @property
    def queue_delay(self) -> float:
        return self.started_at - self.job.ready_at

    @property
    def session_wait(self) -> float:
        return self.started_at - self.lane_ready_at

    @property
    def on_time(self) -> bool:
        return self.queue_delay <= self.job.tolerance


def default_topology(session_cap: int = DEFAULT_SESSION_CAP,
                     lanes: Optional[Mapping[str, int]] = None,
                     label: str = "standalone") -> Topology:
    return Topology(lanes=dict(lanes or DEFAULT_LANES), session_cap=session_cap, label=label)


def grid_topology(nodes: int, lanes: Optional[Mapping[str, int]] = None) -> Topology:
    """A Grid's cap is its node count (one session per node), so `--nodes` is the only knob."""
    return Topology(lanes=dict(lanes or DEFAULT_LANES),
                    session_cap=nodes * GRID_NODE_SESSIONS, label=f"grid/{nodes}-nodes")


def _on_time_pct(results: Sequence[JobResult]) -> Optional[float]:
    if not results:
        return None
    return 100.0 * sum(1 for result in results if result.on_time) / len(results)


def required_topology(users: int, base: Topology, target_on_time_pct: float = 95.0,
                      stagger_hours: Optional[float] = None, specs: Sequence[JobSpec] = WORKLOAD,
                      max_lane_concurrency: int = 64) -> dict:
    """Smallest per-lane concurrency (and therefore session cap) that starts `target_on_time_pct` of
    the day's work inside its window at `users` users.

    Solved **one lane at a time**, which is exact rather than an approximation: §5a's invariant says
    the cap equals the summed lane concurrency, so no lane can ever be blocked by another lane's
    browser — the lanes are independent queues and their answers simply add up. Solving instead for a
    single cap split in today's 3:2:2:1 ratio would inflate the total badly, because the ratio that
    is right for one user's day is not the ratio N users need (se_content's once-a-day batch scales
    with N exactly like se_engage's loops do).

    This — not the instantaneous peak of an unconstrained run — is what "sessions needed" means. The
    arrival pattern is spiky by construction (§3's single crontabs), so the raw peak asks for one
    browser per user; the tolerances say a golden-hour loop has the golden window to start in.
    """
    jobs = build_workload(users, stagger_hours=stagger_hours, specs=specs)
    unknown = {job.lane for job in jobs} - set(base.lanes)
    if unknown:
        raise ValueError(f"workload references lanes with no concurrency configured: {sorted(unknown)}")
    if not jobs:
        return {"cap": None, "lanes": {}, "on_time_pct": None, "target_on_time_pct": target_on_time_pct}
    lanes: dict[str, int] = {}
    for lane in base.lanes:
        lane_jobs = [job for job in jobs if job.lane == lane]
        if not lane_jobs:
            lanes[lane] = 1
            continue
        for concurrency in range(1, max_lane_concurrency + 1):
            solo = Topology(lanes={lane: concurrency}, session_cap=concurrency, label=base.label)
            if (_on_time_pct(simulate(lane_jobs, solo)) or 0.0) >= target_on_time_pct:
                lanes[lane] = concurrency
                break
        else:
            # Unreachable by adding browsers: some job is late no matter what (a 15-minute loop with
            # a 5-minute tolerance cannot start on time behind another copy of itself).
            return {"cap": None, "lanes": {}, "on_time_pct": None,
                    "target_on_time_pct": target_on_time_pct, "searched_to": max_lane_concurrency,
                    "unreachable_lane": lane}
    cap = sum(lanes.values())
    joint = simulate(jobs, Topology(lanes=lanes, session_cap=cap, label=base.label))
    return {"cap": cap, "lanes": lanes, "on_time_pct": _round(_on_time_pct(joint)),
            "target_on_time_pct": target_on_time_pct}


# ───────────────────────────── pure: workload ─────────────────────────────

def _post_time(user_index: int, users: int, band: tuple[int, int]) -> float:
    """Spread users' scheduled posts evenly across the peak band. Deterministic by index so a run is
    reproducible and two topologies can be compared on identical arrivals."""
    start, end = band
    if users <= 1:
        return float(start)
    return start + (end - start) * (user_index / users)


def _staggered_ready(start: float, user_id: int, salt: str, window_minutes: float) -> float:
    """One user's arrival for a fan-out anchored at `start`, reusing the SAME hash
    (`stagger_offset_minutes`) production's `plan_daily_slot` uses — so a load-test user's offset is
    the offset the real beat would give that user_id, not a fresh approximation of it.

    Quantized up to the next `STAGGER_TICK_MINUTES` boundary: the beat only ticks every 15 minutes
    (`daily-golden-hour-engagement` et al. are `crontab(minute='*/15')`), so a slot that lands at
    e.g. +37 minutes is not dispatched until the :45 tick discovers it — modelling a continuous
    offset would understate how many users land on the SAME tick and therefore the same lane
    contention.
    """
    if not window_minutes:
        return float(start)
    raw = start + stagger_offset_minutes(user_id, int(round(window_minutes)), salt=salt)
    return math.ceil(raw / STAGGER_TICK_MINUTES) * STAGGER_TICK_MINUTES


def build_workload(users: int, stagger_hours: Optional[float] = None,
                   specs: Sequence[JobSpec] = WORKLOAD,
                   post_band: tuple[int, int] = POST_BAND) -> list[Job]:
    """The day's Selenium jobs for `users` active users.

    `stagger_hours` overrides EVERY staggerable fan-out's window uniformly — pass `0` to reproduce
    the pre-#554 "everyone at one crontab minute" behaviour, or another value for a what-if. Left at
    the default `None`, each fan-out uses its OWN shipped window (`JobSpec.stagger_window_minutes`:
    golden-hour 180 min, appreciation DMs 120 min, both mirroring `docs/scaling-plan.md` §5d) — this
    is what makes a bare `selenium_load_test.py` run reflect production instead of the fixed-time
    fan-out #554 already replaced (issue #634).
    """
    if users < 0:
        raise ValueError("users must be >= 0")
    if stagger_hours is not None and stagger_hours < 0:
        raise ValueError("stagger_hours must be >= 0")
    override_window = None if stagger_hours is None else stagger_hours * 60.0
    jobs: list[Job] = []
    for index in range(users):
        user_id = index + 1
        for spec in specs:
            if spec.arrival == ARRIVAL_POST:
                ready_times = [_post_time(index, users, post_band) + spec.post_offset_minutes]
            elif not spec.staggerable:
                ready_times = list(spec.starts)
            else:
                window = spec.stagger_window_minutes if override_window is None else override_window
                ready_times = [_staggered_ready(start, user_id, spec.name, window)
                              for start in spec.starts]
            for ready in ready_times:
                jobs.append(Job(user_id=user_id, name=spec.name, lane=spec.lane,
                                ready_at=float(ready), duration=spec.duration_minutes,
                                tolerance=spec.tolerance_minutes))
    return jobs


# ───────────────────────────── pure: simulation ─────────────────────────────

def simulate(jobs: Sequence[Job], topology: Topology) -> list[JobResult]:
    """Discrete-event run of `jobs` through `topology`.

    A task needs BOTH a free lane slot and a free Chrome session, and it holds the lane slot while it
    waits for the session — that coupling is the real system (`--prefetch-multiplier=1` means one
    in-flight task per concurrency slot, blocked or not) and it is why raising a lane without raising
    the cap does nothing. Ties go to whoever queued first, which is how the broker hands them out.
    """
    if not jobs:
        return []
    unknown = {job.lane for job in jobs} - set(topology.lanes)
    if unknown:
        raise ValueError(f"workload references lanes with no concurrency configured: {sorted(unknown)}")
    # A zero would not "run slowly", it would strand every job on that lane forever and quietly
    # report on-time % over the jobs that did run. Refuse the topology instead.
    starved = sorted(lane for lane in {job.lane for job in jobs} if topology.lanes[lane] < 1)
    if starved or topology.session_cap < 1:
        raise ValueError(f"lane concurrency and session cap must be >= 1 (starved lanes: {starved}, "
                         f"cap: {topology.session_cap})")

    pending: dict[str, deque[Job]] = defaultdict(deque)
    for job in sorted(jobs, key=lambda j: (j.ready_at, j.lane, j.user_id, j.name)):
        pending[job.lane].append(job)

    busy_lane: dict[str, int] = defaultdict(int)
    running: list[tuple[float, str]] = []  # heap of (end_time, lane)
    sessions = 0
    lane_ready: dict[int, float] = {}
    results: list[JobResult] = []
    now = min(job.ready_at for job in jobs)

    while any(pending.values()) or running:
        while running and running[0][0] <= now:
            _, lane = heapq.heappop(running)
            busy_lane[lane] -= 1
            sessions -= 1

        while True:
            eligible = [(queue[0].ready_at, lane) for lane, queue in pending.items()
                        if queue and queue[0].ready_at <= now and busy_lane[lane] < topology.lanes[lane]]
            if not eligible:
                break
            # Mark eligibility BEFORE the cap check: a job whose lane slot is free but which cannot
            # get a browser is, from this instant on, waiting on the session cap and nothing else.
            for _, lane in eligible:
                lane_ready.setdefault(id(pending[lane][0]), now)
            if sessions >= topology.session_cap:
                break
            lane = min(eligible)[1]
            job = pending[lane].popleft()
            busy_lane[lane] += 1
            sessions += 1
            heapq.heappush(running, (now + job.duration, lane))
            results.append(JobResult(job=job, started_at=now, lane_ready_at=lane_ready[id(job)]))

        upcoming = [queue[0].ready_at for queue in pending.values() if queue and queue[0].ready_at > now]
        if running:
            upcoming.append(running[0][0])
        if not upcoming:
            break
        now = min(upcoming)
    return results


def concurrency_peak(results: Sequence[JobResult]) -> int:
    """Highest number of simultaneously open sessions across the run."""
    events: list[tuple[float, int]] = []
    for result in results:
        events.append((result.started_at, 1))
        events.append((result.started_at + result.job.duration, -1))
    # -1 before +1 at the same instant: a session that ends exactly when another starts is a handover,
    # not two concurrent browsers.
    events.sort(key=lambda event: (event[0], event[1]))
    peak = current = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def _percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """Nearest-rank — the reported number is one that actually happened, not an interpolation."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(pct * len(ordered))))
    return float(ordered[rank - 1])


def _round(value: Optional[float], digits: int = 1) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def project_resources(sessions: int, topology_label: str) -> dict:
    """What `sessions` concurrent Chrome sessions cost, and whether this VPS can pay it (§5c)."""
    grid = topology_label.startswith("grid")
    chrome_mem = sessions * MEM_PER_SESSION_GB + (sessions * GRID_NODE_OVERHEAD_GB + GRID_HUB_MEM_GB if grid else 0.0)
    cpu = sessions * CPU_PER_SESSION + BASELINE_CPU + (GRID_HUB_CPU if grid else 0.0)
    mem = chrome_mem + BASELINE_MEM_GB
    cpu_pct = cpu / HOST_CPUS * 100
    mem_pct = mem / HOST_MEM_GB * 100
    if cpu_pct <= 100 and mem_pct <= MEM_FITS_PCT:
        verdict = VERDICT_FITS
    elif cpu_pct <= CPU_OVERSUBSCRIPTION_LIMIT * 100 and mem_pct <= MEM_CEILING_PCT:
        verdict = VERDICT_AT_CEILING
    else:
        verdict = VERDICT_EXCEEDS
    return {"chrome_mem_gb": _round(chrome_mem), "total_mem_gb": _round(mem), "host_cpu": _round(cpu, 2),
            "host_cpu_pct": _round(cpu_pct), "host_mem_pct": _round(mem_pct), "verdict": verdict}


def summarize(results: Sequence[JobResult], topology: Topology, users: int,
              stagger_hours: Optional[float] = None, required: Optional[Mapping] = None) -> dict:
    """One row of the on-time / resource curve."""
    delays = [result.queue_delay for result in results]
    waits = [result.session_wait for result in results]
    on_time = [result for result in results if result.on_time]
    peak = concurrency_peak(results)
    required = dict(required or {})
    # `cap` travels as-is, including None: the search returning None means NO session count reaches
    # the target (some lane is late no matter how many browsers it gets), and reporting the simulated
    # peak there would read as a real sizing answer to a cohort decision. `unreachable_lane` says why.
    cap = required.get("cap")
    # Resources are projected for the topology that would MEET the SLO, not the one that is starving:
    # "what would it cost to serve these users" is the question a cohort decision turns on. With no
    # such topology, the peak is the only honest thing left to price — the row is marked unreachable.
    projected = peak if cap is None else cap
    per_lane: dict[str, dict] = {}
    for lane in topology.lanes:
        lane_results = [result for result in results if result.job.lane == lane]
        if not lane_results:
            continue
        per_lane[lane] = {
            "jobs": len(lane_results),
            "on_time_pct": _round(100.0 * sum(1 for r in lane_results if r.on_time) / len(lane_results)),
            "p95_delay_minutes": _round(_percentile([r.queue_delay for r in lane_results], 0.95)),
        }
    return {
        "users": users,
        "topology": topology.label,
        "session_cap": topology.session_cap,
        "lanes": dict(topology.lanes),
        "cap_matches_lanes": topology.matches_invariant,
        "stagger_hours": stagger_hours,
        "jobs": len(results),
        "on_time_pct": _round(100.0 * len(on_time) / len(results)) if results else None,
        "late_jobs": len(results) - len(on_time),
        "queue_delay_p95_minutes": _round(_percentile(delays, 0.95)),
        "worst_delay_minutes": _round(max(delays)) if delays else None,
        "session_wait_p50_minutes": _round(_percentile(waits, 0.50)),
        "session_wait_p95_minutes": _round(_percentile(waits, 0.95)),
        "session_wait_max_minutes": _round(max(waits)) if waits else None,
        "peak_sessions": peak,
        "sessions_needed": cap,
        "unreachable_lane": required.get("unreachable_lane"),
        # What the resource columns below were priced for — equal to sessions_needed unless the SLO
        # is unreachable, in which case it is the simulated peak.
        "projected_sessions": projected,
        "required_lanes": required.get("lanes") or {},
        "required_on_time_pct": required.get("on_time_pct"),
        "target_on_time_pct": required.get("target_on_time_pct"),
        "session_utilisation_pct": _round(100.0 * peak / topology.session_cap) if topology.session_cap else None,
        "per_lane": per_lane,
        **project_resources(projected, topology.label),
    }


def run_scale(users: int, topology: Topology, stagger_hours: Optional[float] = None,
              specs: Sequence[JobSpec] = WORKLOAD, target_on_time_pct: float = 95.0) -> dict:
    """One scale, two answers: what the CURRENT topology delivers, and the smallest topology that
    would hit the SLO. The gap between them is the decision this harness exists to inform."""
    jobs = build_workload(users, stagger_hours=stagger_hours, specs=specs)
    required = required_topology(users, topology, target_on_time_pct=target_on_time_pct,
                                 stagger_hours=stagger_hours, specs=specs)
    return summarize(simulate(jobs, topology), topology, users,
                     stagger_hours=stagger_hours, required=required)


def run_curve(user_counts: Sequence[int], topology: Topology, stagger_hours: Optional[float] = None,
              specs: Sequence[JobSpec] = WORKLOAD, target_on_time_pct: float = 95.0) -> list[dict]:
    return [run_scale(users, topology, stagger_hours=stagger_hours, specs=specs,
                      target_on_time_pct=target_on_time_pct) for users in user_counts]


# ───────────────────────────── pure: rendering ─────────────────────────────

def render_curve(rows: Sequence[Mapping]) -> str:
    """The on-time / resource curve `docs/scaling-plan.md` §5c asks for, as a markdown table that can
    be pasted straight into the plan."""
    if not rows:
        return "No scales simulated."
    first = rows[0]
    lanes = ", ".join(f"{lane} {count}" for lane, count in (first.get("lanes") or {}).items())
    stagger_hours = first.get("stagger_hours")
    stagger_desc = (f"{stagger_hours}h override (uniform)" if stagger_hours is not None
                    else "shipped per-fan-out windows (golden hour 180m, appreciation DMs 120m)")
    header = (f"Topology: {first.get('topology')} — {first.get('session_cap')} session slot(s); "
              f"lanes {lanes}; stagger: {stagger_desc}")
    if not first.get("cap_matches_lanes"):
        header += "  ⚠️ cap != summed lane concurrency (scaling-plan.md §5a invariant)"
    target = first.get("target_on_time_pct")
    lines = [header, "",
             ("| Users | On-time % (today) | Late jobs | Delay p95 | Worst delay | Sessions needed | "
              + "Chrome mem | Host CPU | Verdict |"),
             "|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        needed = row.get("sessions_needed")
        lanes_needed = ", ".join(f"{lane} {count}" for lane, count in (row.get("required_lanes") or {}).items())
        if needed is None:
            # No session count reaches the target on some lane. Printing the fallback number here
            # would be read as a capacity target; say plainly that there isn't one, and drop the
            # lane split — the search returned no lanes to show.
            lane = row.get("unreachable_lane")
            needed_cell = f"unreachable ({lane})" if lane else "unreachable"
        else:
            needed_cell = f"{needed}{f' ({lanes_needed})' if lanes_needed else ''}"
        lines.append(
            f"| {row.get('users')} | {row.get('on_time_pct')}% | {row.get('late_jobs')} | "
            f"{row.get('queue_delay_p95_minutes')} min | {row.get('worst_delay_minutes')} min | "
            f"{needed_cell} | {row.get('chrome_mem_gb')} GB | "
            f"{row.get('host_cpu')} vCPU ({row.get('host_cpu_pct')}%) | {row.get('verdict')} |")
    worst = min(rows, key=lambda row: row.get("on_time_pct") if row.get("on_time_pct") is not None else 100)
    lines += ["", f"Per-lane on-time at the worst scale ({worst.get('users')} users):"]
    for lane, stats in (worst.get("per_lane") or {}).items():
        lines.append(f"  {lane}: {stats.get('on_time_pct')}% on time over {stats.get('jobs')} job(s), "
                     f"p95 delay {stats.get('p95_delay_minutes')} min")
    lines += [
        "",
        (f"\"Sessions needed\" = the smallest per-lane concurrency (and therefore cap) that starts "
         + f"{target}% of the day's work inside its window; the lane split is shown beside it. "
         + "\"unreachable (lane)\" means no session count reaches it on that lane — that row's "
         + "resource columns are priced off the SIMULATED PEAK instead, and are not a sizing target."),
        ("Simulated session wait is 0 whenever the cap covers the lanes: with the §5a invariant held "
         + "(cap == summed lane concurrency) work queues on its LANE, never on a browser. A non-zero "
         + "p95 means the cap is under-provisioned. For the real acquisition wait on a deployed Grid, "
         + "use --live."),
        (f"Projections assume {MEM_PER_SESSION_GB} GB and {CPU_PER_SESSION} vCPU per Chrome session, "
         + f"plus a {BASELINE_CPU} vCPU / {BASELINE_MEM_GB:g} GB app tier, on a {HOST_CPUS} vCPU / "
         + f"{HOST_MEM_GB:g} GB host (docs/scaling-plan.md §1/§4)."),
    ]
    return "\n".join(lines)


def render_live(measured: Mapping) -> str:
    lines = [f"Live Selenium load test — {measured.get('requested')} requested session(s), "
             f"{measured.get('acquired')} acquired, {measured.get('failed')} failed",
             f"  session wait: p50 {measured.get('wait_p50_seconds')}s, "
             f"p95 {measured.get('wait_p95_seconds')}s, max {measured.get('wait_max_seconds')}s",
             f"  grid slots: peak {measured.get('peak_busy_sessions')} busy of "
             f"{measured.get('max_sessions')}",
             f"  host: load peak {measured.get('peak_load1')} on {measured.get('cpus')} core(s), "
             f"RAM available {measured.get('mem_available_min_gb')} GB at the trough "
             f"(~{measured.get('mem_consumed_gb')} GB consumed by the run)"]
    for error in (measured.get("errors") or [])[:5]:
        lines.append(f"  error: {error}")
    return "\n".join(lines)


# ───────────────────────────── live measurement ─────────────────────────────

def _sample_environment() -> dict:
    """One capacity + host sample, reusing the monitor's collectors so live numbers and the
    production alert numbers can never disagree about what "busy" means."""
    from cqc_lem.utilities.capacity_alerts import collect_host_headroom, collect_selenium_capacity
    return {"capacity": collect_selenium_capacity(), "host": collect_host_headroom()}


def _open_and_hold(hold_seconds: float, user_id: Optional[int], index: int) -> dict:
    """Open one real browser session, hold it, close it. Returns the acquisition wait."""
    from cqc_lem.utilities.selenium_util import get_docker_driver, quit_gracefully

    started = time.monotonic()
    driver = None
    try:
        driver = get_docker_driver(session_name=f"LoadTest-{index}", user_id=user_id)
        wait = time.monotonic() - started
        time.sleep(max(0.0, hold_seconds))
        return {"index": index, "wait_seconds": round(wait, 2), "error": None}
    except Exception as e:
        return {"index": index, "wait_seconds": round(time.monotonic() - started, 2), "error": f"{type(e).__name__}: {e}"}
    finally:
        if driver is not None:
            quit_gracefully(driver)


def measure_live_sessions(sessions: int, hold_seconds: float = 30.0,
                          user_id: Optional[int] = None, sample_interval: float = 2.0) -> dict:
    """Open `sessions` REAL concurrent sessions against the hub and measure what it costs.

    This is the validation step Phase 2 gates the cohort on: the simulation says how many slots the
    demand wants, this says whether the deployed Grid can actually hand them out and what the box
    looks like while it does. Deliberately does NOT feed `record_session_wait()` — synthetic waits in
    the monitor's rolling window would file a capacity issue about a load test.
    """
    from concurrent.futures import ThreadPoolExecutor

    if sessions < 1:
        raise ValueError("sessions must be >= 1")
    samples: list[dict] = []
    stop = False

    def _sampler() -> None:
        while not stop:
            try:
                samples.append(_sample_environment())
            except Exception as e:  # a sampler that dies must not take the run with it
                log_warning("Load-test sampler failed", exc=e, task_name="selenium_load_test")
            time.sleep(sample_interval)

    import threading
    sampler = threading.Thread(target=_sampler, daemon=True)
    baseline = _sample_environment()
    sampler.start()
    log_info(f"Opening {sessions} concurrent Selenium session(s) for {hold_seconds}s",
             task_name="selenium_load_test")
    try:
        with ThreadPoolExecutor(max_workers=sessions) as pool:
            outcomes = list(pool.map(lambda index: _open_and_hold(hold_seconds, user_id, index), range(sessions)))
    finally:
        stop = True
        sampler.join(timeout=sample_interval + 1)
    samples.append(_sample_environment())
    return summarize_live(sessions, outcomes, baseline, samples)


def summarize_live(requested: int, outcomes: Sequence[Mapping], baseline: Mapping,
                   samples: Sequence[Mapping]) -> dict:
    """Pure reducer over what `measure_live_sessions` collected — separated so the unit tests can
    pin the arithmetic without a browser."""
    ok = [outcome for outcome in outcomes if not outcome.get("error")]
    waits = [float(outcome["wait_seconds"]) for outcome in ok]
    capacities = [sample.get("capacity") for sample in samples if sample.get("capacity")]
    hosts = [sample.get("host") for sample in samples if sample.get("host")]
    base_host = baseline.get("host") or {}
    mem_floor = min((host.get("mem_available_gb") for host in hosts
                     if host.get("mem_available_gb") is not None), default=None)
    base_mem = base_host.get("mem_available_gb")
    return {
        "requested": requested,
        "acquired": len(ok),
        "failed": len(outcomes) - len(ok),
        "wait_p50_seconds": _round(_percentile(waits, 0.50), 2),
        "wait_p95_seconds": _round(_percentile(waits, 0.95), 2),
        "wait_max_seconds": _round(max(waits), 2) if waits else None,
        "peak_busy_sessions": max((capacity.get("busy_sessions", 0) for capacity in capacities), default=None),
        "max_sessions": max((capacity.get("max_sessions", 0) for capacity in capacities), default=None),
        "peak_load1": max((host.get("load1", 0) for host in hosts), default=None),
        "cpus": base_host.get("cpus"),
        "mem_available_min_gb": mem_floor,
        "mem_consumed_gb": _round(base_mem - mem_floor) if (base_mem is not None and mem_floor is not None) else None,
        "errors": [outcome.get("error") for outcome in outcomes if outcome.get("error")],
    }


# ───────────────────────────── CLI ─────────────────────────────

def parse_lanes(raw: str) -> dict[str, int]:
    """`se_engage=3,se_prepost=2,...` → dict. Explicit so a what-if run can model Phase 2's
    "concurrency 3–4 per lane" without editing compose."""
    lanes: dict[str, int] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, value = part.partition("=")
        if not sep or not value.strip().isdigit():
            raise ValueError(f"bad lane spec {part!r} — expected name=concurrency")
        lanes[name.strip()] = int(value)
    if not lanes:
        raise ValueError("no lanes parsed")
    return lanes


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LEM Selenium concurrency / scale load test")
    parser.add_argument("--users", default="10,50,100",
                        help="comma-separated active-user counts to simulate (default 10,50,100)")
    parser.add_argument("--session-cap", type=int, default=DEFAULT_SESSION_CAP,
                        help=f"Chrome session slots (default {DEFAULT_SESSION_CAP}, the standalone's cap)")
    parser.add_argument("--nodes", type=int, default=None,
                        help="model a Grid of N single-session nodes instead of the standalone")
    parser.add_argument("--lanes", default=None,
                        help="lane concurrencies, e.g. se_engage=4,se_prepost=3,se_outreach=3,se_content=2")
    parser.add_argument("--stagger-hours", type=float, default=None,
                        help="override EVERY staggerable fan-out's window uniformly, in hours "
                             "(0 = pre-#554 single-minute fan-out, for a before/after comparison). "
                             "Default: each fan-out's own shipped window (scaling-plan §5d)")
    parser.add_argument("--target-on-time", type=float, default=95.0,
                        help="on-time %% the 'sessions needed' search must reach (default 95)")
    parser.add_argument("--live", action="store_true",
                        help="ALSO open real concurrent sessions against the hub and measure them")
    parser.add_argument("--live-sessions", type=int, default=4, help="how many real sessions to open")
    parser.add_argument("--hold-seconds", type=float, default=30.0, help="how long to hold each real session")
    parser.add_argument("--live-user-id", type=int, default=None,
                        help="user whose proxy/geo profile the real sessions should use")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = parser.parse_args(argv)

    lanes = parse_lanes(args.lanes) if args.lanes else None
    topology = grid_topology(args.nodes, lanes) if args.nodes else default_topology(args.session_cap, lanes)
    user_counts = [int(part) for part in args.users.split(",") if part.strip()]
    rows = run_curve(user_counts, topology, stagger_hours=args.stagger_hours,
                     target_on_time_pct=args.target_on_time)

    measured = measure_live_sessions(args.live_sessions, hold_seconds=args.hold_seconds,
                                     user_id=args.live_user_id) if args.live else None
    if args.json:
        print(json.dumps({"curve": rows, "live": measured}))
    else:
        print(render_curve(rows))
        if measured:
            print("")
            print(render_live(measured))
    # Exit 2 when the modelled topology cannot serve the largest scale, so a CI/cron caller can gate
    # a cohort onboarding on it without parsing the table.
    return 2 if any(row.get("verdict") == VERDICT_EXCEEDS for row in rows) else 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
