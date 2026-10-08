#!/usr/bin/env bash
# Is the v2 daemon actually working? Run by lem-agentd-watchdog.timer every 15 minutes.
# Installed root-owned in /usr/local/lib/lem by install-root-scripts.sh, not by install.sh.
#
# Deliberately lives OUTSIDE the daemon: an alert inside the process it watches cannot fire when
# that process is what died. And it checks TWO things, because they fail independently —
#
#   1. liveness   — systemd says the unit is active
#   2. freshness  — the heartbeat file is recent
#
# A wedged daemon (deadlocked, stuck on a hung subprocess) satisfies (1) and fails (2), and that is
# the failure mode a naive `systemctl is-active` check reports as healthy. Every unhealthy run
# attempts a restart; while the heartbeat stays stale v1's failsafe cron keeps the pipeline moving,
# so a daemon that will not come back degrades to v1 cadence rather than stalling.
set -uo pipefail

BASE="${BASE:-/home/lem/agent-pipeline}"
HEARTBEAT="$BASE/state/lemd.heartbeat"
STALE_AFTER="${LEMD_HEARTBEAT_STALE:-600}"   # 10 min; the daemon stamps it every pass
LOG="$BASE/logs/watchdog.log"
UNIT="lem-agentd.service"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

active=0
systemctl is-active --quiet "$UNIT" && active=1

now="$(date +%s)"
beat=0
[ -r "$HEARTBEAT" ] && beat="$(cat "$HEARTBEAT" 2>/dev/null || echo 0)"
case "$beat" in ''|*[!0-9]*) beat=0 ;; esac
age=$(( now - beat ))

if [ "$active" = 1 ] && [ "$beat" -gt 0 ] && [ "$age" -lt "$STALE_AFTER" ]; then
  exit 0   # healthy: quiet by design, this runs 96 times a day
fi

if [ "$active" != 1 ]; then
  log "daemon NOT active — restarting $UNIT."
else
  log "daemon active but heartbeat is ${age}s old (stale > ${STALE_AFTER}s) — restarting $UNIT."
fi

# The unit runs this as root (see lem-agentd-watchdog.service), and root restarts the unit directly.
# A non-root caller with no login session gets polkit's "Interactive authentication required" on
# `manage-units`, so that case goes through `sudo -n`. `is-active` above needs no privilege.
if [ "$(id -u)" = "0" ]; then
  restart=(systemctl restart "$UNIT")
else
  restart=(sudo -n systemctl restart "$UNIT")
fi
if ! "${restart[@]}" 2>>"$LOG"; then
  log "restart FAILED (non-root needs NOPASSWD systemctl restart $UNIT) — v1's failsafe cron is now the pipeline."
fi

# One PostHog breadcrumb so a restart loop is visible in the same place every other pipeline
# signal lands. Best-effort: never let telemetry failure change the outcome.
# posthog.sh reads $BASE/secrets.env, which belongs to the runner, so as root the breadcrumb is sent
# as the runner's uid rather than sourcing that file here.
if [ -r "$BASE/lib/posthog.sh" ]; then
  props="{\"active\":$active,\"heartbeat_age_s\":$age,\"unit\":\"$UNIT\"}"
  # shellcheck disable=SC2016
  send='. "$1/lib/posthog.sh" 2>/dev/null && posthog_capture "lemd_watchdog_restart" "agent-pipeline" "$2"'
  if [ "$(id -u)" = "0" ]; then
    setpriv --reuid="${LEM_RUNNER_USER:-lem}" --regid="${LEM_RUNNER_USER:-lem}" --init-groups \
      env BASE="$BASE" bash -c "$send" _ "$BASE" "$props" >/dev/null 2>&1 || true
  else
    bash -c "$send" _ "$BASE" "$props" >/dev/null 2>&1 || true
  fi
fi
