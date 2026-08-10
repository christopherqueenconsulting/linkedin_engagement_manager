#!/usr/bin/env bash
# Hand dispatch to the v2 daemon. The exact inverse of rollback.sh, and idempotent.
#
#   /home/lem/agent-pipeline/v2/cutover.sh
#
# The sentinel is V1_RETIRED, never PAUSED. tick.sh exits unconditionally on PAUSED, so using it as
# the cutover switch would disable the failsafe cron at the exact moment it becomes the only safety
# net (finding C3). PAUSED keeps meaning "a human said stop everything" in both worlds.
#
# The failsafe cron replaces the dispatching one rather than joining it. Two cron lines would mean
# two dispatchers the moment the heartbeat went stale.
set -uo pipefail
B="${BASE:-/home/lem/agent-pipeline}"

echo "=== 1. V1_RETIRED (not PAUSED)"
touch "$B/V1_RETIRED"
[ -f "$B/PAUSED" ] && echo "NOTE: PAUSED is present — a human has stopped BOTH runners; cutover alone will not start work."

echo
echo "=== 2. daemon dispatches"
if grep -q '^LEMD_SHADOW=' "$B/config.env"; then
  sed -i 's/^LEMD_SHADOW=.*/LEMD_SHADOW=0/' "$B/config.env"
else
  printf '\nLEMD_SHADOW=0\n' >> "$B/config.env"
fi
grep '^LEMD_SHADOW=' "$B/config.env"

echo
echo "=== 3. the cron becomes the safety net"
tmp="$(mktemp)"
crontab -l 2>/dev/null | grep -v 'agent-pipeline/tick.sh' > "$tmp"
echo "*/15 * * * * $B/tick.sh --failsafe >/dev/null 2>&1" >> "$tmp"
crontab "$tmp"
rm -f "$tmp"

echo
echo "=== 4. start the daemon"
sudo -n systemctl enable lem-agentd.service >/dev/null 2>&1
sudo -n systemctl restart lem-agentd.service
sleep 10
systemctl is-active lem-agentd.service

echo
echo "=== 5. exactly ONE dispatcher"
crontab -l | grep 'tick.sh' | grep -v -- '--failsafe' \
  && echo "PROBLEM: a dispatching tick cron survives" \
  || echo "cron: failsafe only (correct)"
"$B/tick.sh" 2>&1 | tail -1
