# Agent pipeline v2 — event-driven daemon

> **Status: skeleton.** The state layer, config, capacity model and supervision exist and are
> tested. The scheduler, webhook receiver, `observe()` state machine and agent dispatch land in
> the PRs that follow. Nothing here dispatches work yet, and `LEMD_SHADOW` defaults to on, so an
> installed-but-unconfigured daemon observes and never acts.

## Why v2 exists

v1 (`../tick.sh`) gives one cron tick to ONE unit of work. Measured over 2,456 recorded ticks:

| Symptom | Measurement |
|---|---|
| Ticks spent re-polling GitHub for unchanged answers | **75–84%** (1,103 of 1,464 dispatches) |
| Hard ceiling on work, regardless of `MAX_AGENTS=5` | 288 units/day (one per 5-min tick) |
| Concurrency actually used | slot ≥2 on **39 of 2,456** ticks |
| Worst single incident | one wedged PR consumed **45 of 62** ticks in 6 hours |

Meanwhile the thing being polled is fast: PR CI ~3 min, merge queue median 3.8 min. The pipeline
was never GitHub-bound — it was tick-bound.

v2 keeps every guard v1 earned (trust boundary, worktree isolation, merge gate, RUNBOOK contracts)
and changes only *when* work runs: a long-lived scheduler with explicit wait states, woken by
GitHub webhooks, reconciling on a slow timer so a missed delivery is a delay and never a loss.

## Layout

```
v2/
  lemd/
    db.py        SQLite state: items, events, runs, kv. Atomic claims, crash recovery.
    config.py    Reads the SAME config.env v1 uses (one file, one PAUSED switch).
    capacity.py  Concurrency caps — v1's CAP arithmetic, plus a separate pool for gh-only work.
  systemd/       daemon + watchdog units
  watchdog.sh    liveness AND heartbeat freshness, from outside the daemon
```

## Design decisions worth knowing

**SQLite, not the app's MySQL.** The pipeline ships the app; coupling its liveness to the app stack
means a deploy can stall the thing performing the deploy. The DB is disposable — every row is
re-derivable from GitHub, so corruption is handled by deleting and reconciling.

**Budgets are NOT in this database.** They stay in the TSV ledger (`../lib/ledger.sh`) so v1 and v2
read the same counters byte-for-byte during migration. Two sources of truth for a budget is how you
get one PR parked at 2 attempts and another retried forever.

**Claims are an index, not a lock.** `items_active_branch` is a partial unique index over
`branch WHERE state IN ('claimed','running')`, so "two workers on one branch" is unrepresentable
rather than merely guarded. v1 needed `flock`s and could still re-arm auto-merge on a PR another
slot was parking.

**Wait states are invisible to the scheduler.** An item awaiting CI, review, the merge queue, or a
human costs zero attention until an event marks it dirty or its TTL fires. This is the entire
economy change, and it is also what removes head-of-line blocking: a waiting PR is not a candidate,
so it cannot starve the PRs behind it.

**Shadow-first.** `LEMD_SHADOW=1` (the default) means observe, decide, log — mutate nothing. The
migration runs here for ≥3 days and is accepted on a replay criterion, not an agreement percentage:
v1's per-tick observed inputs are replayed through v2's decision function offline, requiring zero
safety violations and human review of every action v2 would have taken that v1 did not.

## Operating

```bash
sudo systemctl status lem-agentd            # is it running
tail -f /home/lem/agent-pipeline/logs/lemd.log
sqlite3 /home/lem/agent-pipeline/v2/state/queue.db 'SELECT state, COUNT(*) FROM items GROUP BY state'
touch /home/lem/agent-pipeline/PAUSED       # stops BOTH v1 and v2
```

The watchdog timer restarts a dead *or wedged* daemon every 15 minutes. If it cannot, v1's failsafe
cron takes over at v1 cadence — degraded, never stalled.
