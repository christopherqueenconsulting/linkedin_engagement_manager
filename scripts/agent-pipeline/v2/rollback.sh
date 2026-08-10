#!/usr/bin/env bash
# Return dispatch to v1. The whole rollback, in one command, in under five minutes.
#
#   /home/lem/agent-pipeline/v2/rollback.sh
#
# Three steps, and the ORDER is the design:
#
#   1. SIGTERM the daemon. That is DRAIN, not kill: it stops claiming new work and lets running
#      children finish. Killing them would abandon an agent's commits in a worktree that v1 is
#      about to pick up — the budget already charged, the work already done, and no record of it.
#   2. Remove V1_RETIRED. Every tick from this instant behaves normally again, including a
#      `--failsafe` one (with the sentinel gone, the failsafe block does not run at all).
#   3. Restore the 5-minute cron. Without this, rollback still works but at the failsafe's 15-minute
#      cadence, which is a third of v1's throughput and would look like the rollback had failed.
#
# What makes this safe to run at any moment: v1 and v2 share the state formats. Budgets live in the
# same TSV ledger, branches are claimed with the same `br-*` flocks, and PAUSED means the same thing
# to both. There is nothing to translate back, so nothing to lose.
#
# PAUSED is NOT touched. It is the human stop switch, and a rollback is not a stop.
set -uo pipefail
B="${BASE:-/home/lem/agent-pipeline}"

echo "=== 1. drain the daemon (SIGTERM — running agents finish)"
sudo -n systemctl stop lem-agentd.service
systemctl is-active lem-agentd.service || true

echo
echo "=== 2. hand dispatch back to v1"
rm -f "$B/V1_RETIRED"
[ -e "$B/V1_RETIRED" ] && echo "PROBLEM: V1_RETIRED still present" || echo "V1_RETIRED removed"

echo
echo "=== 3. restore the 5-minute cron"
tmp="$(mktemp)"
crontab -l 2>/dev/null | grep -v 'agent-pipeline/tick.sh' > "$tmp"
echo "*/5 * * * * $B/tick.sh >/dev/null 2>&1" >> "$tmp"
crontab "$tmp"
rm -f "$tmp"
crontab -l | grep 'tick.sh'

echo
echo "=== 4. prove v1 dispatches again"
DRY_RUN=1 "$B/tick.sh" 2>&1 | tail -3

echo
echo "Rolled back. To return to v2:  $B/v2/cutover.sh"
