"""The scheduler daemon.

One loop: drain events, re-observe what changed, expire TTLs, then act within the caps. It replaces
v1's model of "one cron tick does one thing and exits", whose real ceiling was one unit of work per
5 minutes regardless of how many slots were configured.

Shadow mode (`LEMD_SHADOW=1`, the default) runs the entire loop and logs every decision it WOULD
take without touching GitHub or spawning anything. That is the migration's safety net and its
acceptance evidence: the decision stream can be diffed against what v1 actually did before anything
is allowed to act.

Dispatch itself is intentionally NOT here yet — this PR lands the loop, the observation cycle and
the wait-state economy. Agent execution arrives next, so that a daemon which is running but
mis-deciding can be caught while it is still incapable of doing anything.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from typing import Any

from . import capacity, db, github, observe
from .config import Config, load

LOG = logging.getLogger("lemd")

#: The scheduler sleeps at most this long even with nothing due, so a missed wake-up degrades to a
#: short delay rather than a stall.
MAX_SLEEP = 60


class Daemon:
    """Owns the queue, the loop, and the decision to act or merely observe."""

    def __init__(self, cfg: Config) -> None:
        """Open state and prepare the loop (no I/O against GitHub yet)."""
        self.cfg = cfg
        self.conn = db.connect(cfg.db_path)
        self._stop = False
        self._last_reconcile = 0.0
        self._decisions = 0

    # ---------------------------------------------------------------- lifecycle

    def request_stop(self, *_: Any) -> None:
        """Drain: stop claiming new work and let the loop exit cleanly."""
        LOG.info("stop requested — draining")
        self._stop = True

    def startup(self) -> None:
        """Recover crash state, then reconcile once before deciding anything.

        Order matters: recovery first so a stranded claim is released before the reconciler can
        observe the item and reason about it, and reconcile before dispatch so the first pass acts
        on GitHub's truth rather than a stale queue.
        """
        stats = db.startup_recover(self.conn)
        LOG.info(
            "startup: %s runs closed, %s claims released, shadow=%s, db=%s",
            stats["runs_closed"], stats["claims_released"], self.cfg.shadow, self.cfg.db_path,
        )
        db.kv_set(self.conn, "daemon_started_at", str(int(time.time())))

    # ---------------------------------------------------------------- inputs

    def drain_events(self) -> int:
        """Fold webhook deliveries into item dirt-flags.

        Events never decide anything; they only say "this item changed, look again". That is what
        makes a duplicate or out-of-order delivery harmless — the re-observation is the authority.
        """
        rows = db.unprocessed_events(self.conn, limit=200)
        if not rows:
            return 0
        seen: set[tuple[str, int]] = set()
        for row in rows:
            number = row["number"]
            if not number:
                continue
            kind = "pr" if row["event"] in {
                "pull_request", "pull_request_review", "pull_request_review_thread",
                "check_suite", "merge_group",
            } else "issue"
            if db.get_item(self.conn, kind, number) is not None:
                db.mark_dirty(self.conn, kind, number)
            seen.add((kind, number))
        db.mark_events_processed(self.conn, [r["id"] for r in rows])
        LOG.info("drained %s event(s) touching %s item(s)", len(rows), len(seen))
        return len(rows)

    def reconcile(self) -> int:
        """Re-derive the queue from GitHub. The backstop that makes missed events harmless.

        Deliberately a handful of list calls, not a per-item walk: v1 spent 40-70 API calls per tick
        largely by asking the same questions per PR per lane.
        """
        found = 0
        try:
            for label, kind in (
                ("agent:ready", "issue"),
                ("agent:working", "pr"),
                ("agent:revise", "pr"),
                ("agent:depfix", "pr"),
                ("agent:docfix", "pr"),
            ):
                for obj in github.list_by_label(self.cfg.slug, label, kind):
                    number = obj.get("number")
                    if not number:
                        continue
                    existing = db.get_item(self.conn, kind, number)
                    db.upsert_item(
                        self.conn, kind=kind, number=number,
                        state=existing["state"] if existing else db.STATE_READY,
                        branch=obj.get("headRefName"),
                        head_sha=obj.get("headRefOid"),
                        labels_json=json.dumps(sorted(github.label_names(obj))),
                        dirty=1,
                    )
                    found += 1
        except github.GitHubUnavailable as exc:
            # A failed reconcile is not a reason to act on stale state; try again next pass.
            LOG.warning("reconcile incomplete: %s", exc)
        self._last_reconcile = time.time()
        db.kv_set(self.conn, "last_reconcile_at", str(int(self._last_reconcile)))
        return found

    # ---------------------------------------------------------------- decisions

    def observe_dirty(self, limit: int = 25) -> int:
        """Re-observe changed items and apply their decisions.

        Bounded per pass so one busy minute cannot monopolise the loop and starve the TTL sweep.
        """
        rows = self.conn.execute(
            "SELECT * FROM items WHERE dirty=1 AND state NOT IN (?, ?) LIMIT ?",
            (db.STATE_MERGED, db.STATE_CLOSED, limit),
        ).fetchall()
        for row in rows:
            self._observe_one(row)
        return len(rows)

    def sweep_ttls(self, limit: int = 25) -> int:
        """Re-check items whose wait deadline expired."""
        rows = db.due_items(self.conn)[:limit]
        for row in rows:
            self._observe_one(row)
        return len(rows)

    def _observe_one(self, row) -> None:
        """Snapshot one item, decide, and record the outcome."""
        kind, number = row["kind"], row["number"]
        if kind == "pr":
            snap = observe.snapshot_pr(self.cfg.slug, number)
        else:
            snap = observe.snapshot_issue(self.cfg.slug, number)

        decision = observe.decide(
            snap,
            ttl_ci=self.cfg.ttl_ci, ttl_review=self.cfg.ttl_review,
            ttl_queue=self.cfg.ttl_queue, ttl_parked=self.cfg.ttl_parked,
        )
        self._decisions += 1
        self._emit(row, snap, decision)

        now = int(time.time())
        wake_at = now + decision.wake_in if decision.wake_in else None

        if self.cfg.shadow:
            # Record the observation, never the transition: shadow mode must not move items, or a
            # rollback to v1 would inherit a queue v1 never built.
            db.upsert_item(
                self.conn, kind=kind, number=number, state=row["state"],
                head_sha=snap.head_sha or row["head_sha"],
                branch=snap.branch or row["branch"], dirty=0,
            )
            return

        if decision.action == observe.ACT_DISPATCH:
            # Dispatch lands in the next PR. Until then the decision is recorded and the item is
            # left dispatchable, so nothing is lost by the daemon being unable to act yet.
            db.upsert_item(self.conn, kind=kind, number=number, state=db.STATE_READY, dirty=0)
            return

        db.upsert_item(
            self.conn, kind=kind, number=number, state=decision.next_state,
            wait_reason=decision.wait_reason, parked_reason=decision.park_reason,
            wake_at=wake_at, head_sha=snap.head_sha or row["head_sha"],
            branch=snap.branch or row["branch"], dirty=0,
        )

    def _emit(self, row, snap: observe.Snapshot, decision: observe.Decision) -> None:
        """One structured line per decision — the shadow phase's entire evidence base."""
        payload = {
            "ts": int(time.time()),
            "shadow": self.cfg.shadow,
            "kind": row["kind"], "number": row["number"],
            "from_state": row["state"], "to_state": decision.next_state,
            "action": decision.action, "mode": decision.mode, "reason": decision.reason,
            "wake_in": decision.wake_in, "readable": snap.readable,
            **({"details": decision.details} if decision.details else {}),
        }
        LOG.info("decision %s", json.dumps(payload, separators=(",", ":")))
        try:
            path = self.cfg.base / "logs" / "lemd-decisions.ndjson"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except OSError as exc:  # telemetry must never break the loop
            LOG.warning("could not write decision log: %s", exc)

    # ---------------------------------------------------------------- the loop

    def next_sleep(self) -> float:
        """Seconds until the next thing is due, bounded."""
        row = self.conn.execute(
            "SELECT MIN(wake_at) AS w FROM items WHERE wake_at IS NOT NULL"
        ).fetchone()
        due_in = (row["w"] - time.time()) if row and row["w"] else MAX_SLEEP
        until_reconcile = (self._last_reconcile + self.cfg.reconcile_interval) - time.time()
        return max(1.0, min(MAX_SLEEP, due_in, until_reconcile))

    def tick(self) -> None:
        """One pass of the loop."""
        capacity.heartbeat(self.cfg.heartbeat_file)
        if self.cfg.is_paused():
            LOG.info("PAUSED file present — observing nothing this pass")
            return
        self.drain_events()
        if time.time() - self._last_reconcile >= self.cfg.reconcile_interval:
            self.reconcile()
        self.observe_dirty()
        self.sweep_ttls()

    def run(self) -> None:
        """Loop until asked to stop."""
        self.startup()
        while not self._stop:
            started = time.time()
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must outlive any single bad pass
                LOG.exception("tick failed; continuing")
            elapsed = time.time() - started
            if elapsed > 30:
                LOG.warning("slow pass: %.1fs", elapsed)
            time.sleep(self.next_sleep())
        LOG.info("stopped after %s decisions", self._decisions)


def main() -> None:
    """Entry point for `python -m lemd.daemon`."""
    logging.basicConfig(
        level=os.environ.get("LEMD_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = load()
    daemon = Daemon(cfg)
    signal.signal(signal.SIGTERM, daemon.request_stop)
    signal.signal(signal.SIGINT, daemon.request_stop)
    LOG.info(
        "lemd starting: slug=%s shadow=%s agents=%s gh=%s",
        cfg.slug, cfg.shadow, cfg.max_agents, cfg.gh_slots,
    )
    daemon.run()


if __name__ == "__main__":
    main()
