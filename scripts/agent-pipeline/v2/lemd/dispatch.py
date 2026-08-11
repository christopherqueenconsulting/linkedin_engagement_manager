"""Execution: the daemon's supervisor for the bash actions that actually touch GitHub.

The daemon decides; nothing here decides. This module owns process lifecycle only — two bounded
pools, spawn, reap, and kill-on-deadline — and it deliberately performs no GitHub call and no trust
evaluation of its own. Both of those live in `actions/*.sh`, which source the same
incident-hardened `lib/guards.sh` that v1 runs on, so there is exactly one implementation of the
trust boundary rather than a Python paraphrase of it.

Two pools, because they have nothing in common:

* `agent_slots` — `claude -p` runs. Minutes to tens of minutes, and the scarce resource is the
  subscription, not the box.
* `gh_slots` — arming auto-merge, parking, labels. Seconds, no model spend. Separate so a merge
  never queues behind a 20-minute implementation run, which is a large part of how v1 turned a
  3.8-minute merge queue into hours of latency.

Every child is started in its own process GROUP (`start_new_session`), and the deadline is enforced
with `killpg`. Killing only the direct child would leave the `claude` process it spawned running
against the same worktree, which is the shape of a double-dispatch: the item is reaped, re-observed
and re-dispatched while the original agent is still writing to the branch.
"""

from __future__ import annotations

import fcntl
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import db, policy

LOG = logging.getLogger("lemd.dispatch")

#: Exit codes the actions use to say WHY they refused. Distinct on purpose: a trust refusal and a
#: flaky run must never be retried the same way, and v1's habit of collapsing both into "non-zero"
#: is how a refused item got re-attempted every five minutes forever.
EX_TRUST = 70
EX_BUDGET = 71
EX_BUSY = 72
EX_SETUP = 73
REFUSALS = frozenset({EX_TRUST, EX_BUDGET, EX_BUSY, EX_SETUP})


@dataclass
class Child:
    """One running action."""

    proc: subprocess.Popen
    pool: str
    mode: str
    kind: str
    number: int
    item_id: int | None
    run_id: int | None
    deadline: float
    started: float
    log_path: Path | None = None
    killed: bool = False
    env: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def label(self) -> str:
        """Short identity for log lines."""
        return f"{self.mode}:{self.kind}#{self.number}"


class Supervisor:
    """Bounded, non-blocking execution of the action scripts."""

    def __init__(self, cfg, conn) -> None:
        """Bind to the daemon's config and queue connection."""
        self.cfg = cfg
        self.conn = conn
        self.children: list[Child] = []
        self.actions = Path(cfg.base) / "v2" / "actions"

    # ------------------------------------------------------------------ coexistence

    def v1_slots_busy(self) -> int:
        """How many v1 ticks are still running.

        Cutover flips a sentinel, but a tick already in flight can run for 45 minutes past it (M10).
        Its agents are doing real work and its slot lock is held for the duration, so the daemon
        holds its own dispatch until they drain rather than adding three concurrent agents on top of
        three that are already running — twice the envelope this box has been measured at.

        Correctness does not depend on this: both runners take the same `br-*` flock per branch, so
        neither can touch the other's work. This bounds CPU, and it is enforced here rather than by
        an operator remembering to wait.
        """
        busy = 0
        for lock in sorted((Path(self.cfg.base) / "locks").glob("slot-*.lock")):
            try:
                fd = os.open(lock, os.O_RDWR)
            except OSError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                busy += 1
            finally:
                os.close(fd)
        return busy

    # ------------------------------------------------------------------ capacity

    def in_pool(self, pool: str) -> int:
        """How many runs of one pool are in flight — including ones this process did not spawn.

        The in-memory child list is NOT sufficient. Children are launched into their own sessions so
        they outlive a daemon restart (that is deliberate: a restart must not abandon an agent
        mid-`git push`), but `self.children` is empty after one. The cap was therefore computed as
        if nothing were running, and every restart granted a fresh full pool on top of the agents
        still working — measured at 5 runs against a cap of 3 after three restarts in ten minutes.

        The `runs` table is the durable answer, and `_pid_alive` pairs pid with start time so a
        recycled pid cannot make a dead run count forever.
        """
        live = sum(1 for c in self.children if c.pool == pool)
        adopted = 0
        for row in self.conn.execute(
            "SELECT mode, pid, pid_start FROM runs WHERE ended_at IS NULL AND pid IS NOT NULL"
        ).fetchall():
            if any(c.proc is not None and c.proc.pid == row["pid"] for c in self.children):
                continue  # already counted above
            if not db.pid_alive(int(row["pid"]), row["pid_start"]):
                continue
            # Keep this set in step with `act()`'s pool routing. A gh action counted as an AGENT
            # here would let a restart adopt it against the wrong cap — the agent pool is the
            # scarce one, and mis-attributing a two-second label write to it stalls a real run.
            row_pool = "gh" if row["mode"] in ("merge_enable", "park", "unpark", "disarm") else "agent"
            if row_pool == pool:
                adopted += 1
        return live + adopted

    def free(self, pool: str) -> int:
        """Slots left in one pool."""
        cap = self.cfg.max_agents if pool == "agent" else self.cfg.gh_slots
        return max(0, cap - self.in_pool(pool))

    @property
    def occupancy(self) -> float:
        """Agent-pool utilisation, 0.0-1.0 — the input to the timeout stretch (M6)."""
        cap = max(1, self.cfg.max_agents)
        return min(1.0, self.in_pool("agent") / cap)

    # ------------------------------------------------------------------ spawning

    def _spawn(self, argv: list[str], *, pool: str, mode: str, kind: str, number: int,
               item_id: int | None, timeout_s: int) -> Child | None:
        """Start one action detached into its own process group."""
        logdir = Path(self.cfg.base) / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        log_path = logdir / f"v2-{mode}-{kind}{number}-{int(time.time())}.log"
        env = dict(os.environ)
        env.update({
            "BASE": str(self.cfg.base),
            "REPO": str(self.cfg.repo),
            "SLUG": self.cfg.slug,
            "LEMD_BUDGET": str(policy.budget_for(mode)),
            # run_lane's own `timeout` sits UNDER the daemon's killpg, deliberately a minute longer:
            # if both fire the daemon's is the one that reaps the whole group, and a race between
            # two kill paths at the same instant produces confusing half-dead states.
            "CLAUDE_TIMEOUT": f"{max(1, timeout_s // 60) + 1}m",
            # Where the child reports the lane it actually chose. The run row is opened here, but
            # routing happens inside run_lane.sh — so without a channel back, `runs.lane` /
            # `model` / `route_reason` stay NULL for ever, and they did on all 59 rows written on
            # cutover day.
            "LEMD_RUN_META": f"{log_path}.meta",
        })
        try:
            handle = log_path.open("a")
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                argv, stdout=handle, stderr=subprocess.STDOUT, env=env,
                cwd=str(self.cfg.base), start_new_session=True,
            )
        except OSError as exc:
            LOG.error("could not spawn %s for %s#%s: %s", mode, kind, number, exc)
            return None

        run_id = None
        if item_id is not None:
            run_id = db.start_run(
                self.conn, item_id=item_id, mode=mode, pid=proc.pid,
                log_path=str(log_path), timeout_s=timeout_s,
            )
        child = Child(
            proc=proc, pool=pool, mode=mode, kind=kind, number=number, item_id=item_id,
            run_id=run_id, deadline=time.time() + timeout_s, started=time.time(),
            log_path=log_path,
        )
        self.children.append(child)
        LOG.info("spawned %s pid=%s timeout=%ss log=%s", child.label, proc.pid, timeout_s, log_path)
        return child

    def dispatch_agent(self, *, mode: str, kind: str, number: int, branch: str | None,
                       item_id: int | None) -> Child | None:
        """Launch an agent run for one item."""
        if self.free("agent") <= 0:
            return None
        timeout_s = policy.timeout_for(mode, occupancy=self.occupancy)
        argv = [str(self.actions / "agent_run.sh"), mode, kind, str(number), branch or ""]
        return self._spawn(argv, pool="agent", mode=mode, kind=kind, number=number,
                           item_id=item_id, timeout_s=timeout_s)

    def dispatch_gh(self, *, action: str, kind: str, number: int, args: list[str],
                    item_id: int | None) -> Child | None:
        """Launch a cheap gh mutation (merge-enable, park, unpark)."""
        if self.free("gh") <= 0:
            return None
        script = self.actions / f"{action}.sh"
        # 180s: these are single API calls plus at most 40s of documented backoff. A gh mutation
        # that has not returned in three minutes is wedged, and holding a gh slot open for it
        # starves the lane that exists to be fast.
        return self._spawn([str(script), *args], pool="gh", mode=action, kind=kind,
                           number=number, item_id=item_id, timeout_s=180)

    # ------------------------------------------------------------------ reaping

    def reap(self) -> list[tuple[Child, int]]:
        """Collect finished children and kill any that overran.

        Returns:
            `(child, rc)` for each child that ended this pass. A timed-out child reports `RC_KILLED` so
            the caller can tell "the agent decided it failed" from "we stopped it", which is what
            makes a starvation-park auditable rather than indistinguishable from a real failure.
        """
        done: list[tuple[Child, int]] = []
        now = time.time()
        self._reap_adopted(now)
        for child in list(self.children):
            rc = child.proc.poll()
            if rc is None:
                if now >= child.deadline and not child.killed:
                    self._kill(child)
                continue
            self.children.remove(child)
            if child.run_id is not None:
                self._record_lane(child)
                db.finish_run(self.conn, child.run_id, rc)
            LOG.info("%s finished rc=%s in %.0fs%s", child.label, rc, now - child.started,
                     " (killed on deadline)" if child.killed else "")
            done.append((child, rc))
        return done

    def _record_lane(self, child: Child) -> None:
        """Fold the child's reported lane into its run row.

        Best-effort by construction: a child that died before writing leaves the columns NULL,
        which is exactly the state before this existed. Attribution telemetry must never be able to
        fail a reap — losing a lane label costs a data point, losing a reap leaks a slot.
        """
        meta = Path(f"{child.log_path}.meta")
        try:
            fields = dict(
                line.split("=", 1)
                for line in meta.read_text().splitlines() if "=" in line
            )
        except OSError:
            return
        finally:
            meta.unlink(missing_ok=True)
        db.set_run_lane(
            self.conn, child.run_id,
            lane=fields.get("lane") or None,
            model=fields.get("model") or None,
            route_reason=fields.get("route_reason") or None,
        )

    def _reap_adopted(self, now: float) -> None:
        """Close run rows whose process is gone but which this daemon never spawned.

        Closed with `RC_VANISHED`, never `RC_KILLED`. The two are different events — we stopped it
        vs it was already gone — and the whole reason the supervisor uses a distinct code at all is
        so a reader can tell "the agent decided it failed" from "we stopped it". An orphan closed as
        a kill makes the start lane look like it is timing out when it is not.

        The counterpart to counting them in `in_pool`. `startup_recover` closes dead runs at start,
        but a run adopted by a LONG-LIVED daemon has no other closer — so an orphan that exited five
        minutes after a restart would hold its slot until the next restart, and the pool would shrink
        by one every time the daemon was bounced. Its item is marked dirty so the next observation
        decides its fate from GitHub, which is the only place the answer actually lives.
        """
        mine = {c.proc.pid for c in self.children if c.proc is not None}
        for row in self.conn.execute(
            "SELECT id, item_id, pid, pid_start FROM runs WHERE ended_at IS NULL AND pid IS NOT NULL"
        ).fetchall():
            if row["pid"] in mine or db.pid_alive(int(row["pid"]), row["pid_start"]):
                continue
            LOG.info("adopting and closing orphaned run %s (pid %s is gone)", row["id"], row["pid"])
            db.finish_run(self.conn, row["id"], db.RC_VANISHED, now=int(now))
            if row["item_id"] is not None:
                db.force_state(self.conn, row["item_id"], db.STATE_READY, dirty=1,
                               pending_mode=None, wake_at=None, now=int(now))

    def _kill(self, child: Child) -> None:
        """Stop an overrunning child and everything it spawned."""
        child.killed = True
        LOG.warning("%s exceeded its deadline — killing process group %s",
                    child.label, child.proc.pid)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(child.proc.pid), sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                child.proc.wait(timeout=10)
                return
            except subprocess.TimeoutExpired:
                continue

    def drain(self, *, timeout: float = 0.0) -> None:
        """Stop claiming and let running children finish — the rollback path.

        Children are deliberately NOT killed: the <5-minute rollback is "stop starting work, let
        what is in flight land", so that a mid-flight agent's commits are not abandoned in a
        worktree while v1 takes over the same branch.
        """
        deadline = time.time() + timeout
        while self.children and (timeout <= 0 or time.time() < deadline):
            self.reap()
            if self.children:
                time.sleep(1.0)
        if self.children:
            LOG.info("draining: %s child(ren) still running, leaving them to finish",
                     len(self.children))
