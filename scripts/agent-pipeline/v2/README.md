# Agent pipeline v2 — operator card

This file ships to the box (`install.sh` copies `v2/*.md`), so it is the card you read at the
console. **The design, the state machine and the decision table live in
[`docs/agent-pipeline-v2.md`](../../../docs/agent-pipeline-v2.md)** in the repo — read that to
understand what the daemon does and why. This one is commands.

**Live since 2026-08-10:** `LEMD_SHADOW=0`, `V1_RETIRED` present, `lem-agentd` enabled. v1
(`../tick.sh`) runs only as a heartbeat-gated 15-minute failsafe.

## Is it healthy?

```bash
systemctl status lem-agentd lem-agent-webhook   # alive?
cat state/lemd.heartbeat                        # ...and not wedged (age < 600s)
../status.sh                                    # the whole picture
../status.sh --watch                            # ...refreshed
```

Liveness and freshness are different questions. A wedged process passes `is-active` and fails the
heartbeat; `lem-agentd-watchdog.timer` checks both every 15 minutes and restarts on either.

**Running is not the same as running the code you merged.** Both units import the `lemd` package and
keep whatever they imported at their own start, so one that was never restarted after a sync serves
code that is no longer on disk — the receiver did that for 23 hours (#1412). `status.sh` compares
each unit's start time against the newest file in `v2/lemd/` and lists any that lose, under
`stale units:` and in NEEDS ATTENTION (`--json`: `v2.stale_units`).

## What is it doing?

```bash
tail -f logs/lemd.log                                   # the loop
jq -c 'select(.stage=="observe")' logs/lemd-decisions.ndjson | tail -20   # every decision + reason
jq -c 'select(.stage=="act" and .executed)' logs/lemd-decisions.ndjson | tail
sqlite3 v2/state/queue.db \
  'select kind,number,state,pending_mode,parked_reason from items where state not in ("merged","closed")'
```

Every decision carries a `reason`; the full list and what each means is §4 of the design doc.

## Controls

```bash
touch ../PAUSED             # stop EVERYTHING (v1 and v2 both honour it)
rm ../PAUSED                # resume
sudo systemctl restart lem-agentd lem-agent-webhook   # BOTH — both load `lemd`
./rollback.sh               # hand dispatch back to v1 (drains, does not kill children)
./cutover.sh                # ...and back to v2 (idempotent)
```

`LEMD_HOLD_STARTS=1` in `../config.env` holds only the START lane — merge, park and selfreview keep
running so in-flight PRs still drain. Capping `LEMD_MAX_AGENTS` to 0 would starve selfreview too,
which is the merge gate's evidence source, so the queue would wedge behind the lane you meant to keep.

**`PAUSED` is not `V1_RETIRED`.** `PAUSED` stops both runners; `V1_RETIRED` demotes v1 to the
failsafe. `cutover.sh` writes the latter deliberately — `tick.sh` exits unconditionally on `PAUSED`,
so using it would disable the failsafe as well.

## Deploying a change

The pipeline is **not in the Docker image** and no workflow ships it. From the repo checkout:

```bash
scripts/agent-pipeline/install.sh --sync                 # only files the box has not edited
sudo systemctl restart lem-agentd lem-agent-webhook      # BOTH — required for v2/lemd/*.py changes
```

**Restart both.** The receiver loads the same `lemd` package as the daemon and was not named here,
so it ran 23-hour-old code through nine merged changes (#1412) — visible only as a
`kv.schema_version` that would not advance, because `receiver.py` calls `db.connect()` per request
and every delivery rewrote the version from its stale module. `sync.sh` restarts both; a hand sync
has to. Then check `../status.sh` — a unit that was missed is listed under `stale units:`.

A file the box has edited is refused, not overwritten; read the printed `diff` before reaching for
`--sync --force`.

## Prerequisite the watchdog needs

The watchdog runs `sudo -n systemctl restart lem-agentd.service`. The non-login `lem` user cannot
restart a unit through polkit, so that exact command needs a sudoers rule — without it the watchdog
detects a dead daemon and cannot do anything about it.
