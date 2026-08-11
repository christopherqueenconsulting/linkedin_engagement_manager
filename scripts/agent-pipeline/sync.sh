#!/usr/bin/env bash
# Bring the installed pipeline up to date with `main`, and prove it still runs.
#
# The pipeline is NOT in the Docker image and no workflow deploys it: until now it reached the VPS
# only when a human ran `install.sh --sync` by hand. So `main` and the running pipeline could
# diverge silently, and did — the webhook receiver ran 23-hour-old code through nine merged changes
# before anyone noticed (#1412).
#
# PULL, not push, and the reason is the credential. A CI push needs a key that executes as the `lem`
# uid — the uid holding `secrets.env`, the App token, the worktrees and every agent run. A pull needs
# nothing inbound: the box reads a public repo over HTTPS. It also keeps working when Actions is down
# or the runner cannot reach the box, and it can refuse and roll itself back, which a runner cannot.
#
# Everything here is arranged so the FAILURE modes are boring:
#
#   * `state/SYNC_HOLD` stops the updater itself. The kill switch has to be outside the thing it
#     kills, or a bad sync can take away the means to stop the next one.
#   * `install.sh --sync` runs WITHOUT `--force`. Its refusal on a box-edited file is a safety
#     property, not an obstacle: it means someone is mid-debug on this machine, and overwriting them
#     is how you lose an investigation. A refusal stops the run loudly and restarts nothing.
#   * The install is snapshotted first and restored if the daemon does not come back.
#   * Only `main` is ever deployed, and only a commit that is an ancestor of it.
set -uo pipefail

BASE="${BASE:-/home/lem/agent-pipeline}"
SRC="${LEM_SYNC_SRC:-/home/lem/agent-pipeline-src}"
SNAPSHOTS="${LEM_SYNC_SNAPSHOTS:-$BASE/state/sync-snapshots}"
KEEP="${LEM_SYNC_KEEP:-3}"
VERIFY_SECONDS="${LEM_SYNC_VERIFY_SECONDS:-90}"
LOG="${LEM_SYNC_LOG:-$BASE/logs/sync.log}"
UNITS="${LEM_SYNC_UNITS:-lem-agentd.service lem-agent-webhook.service}"

mkdir -p "$(dirname "$LOG")" "$SNAPSHOTS" "$BASE/state" 2>/dev/null
log() { echo "[$(date '+%F %T')] [sync] $*" | tee -a "$LOG"; }

# Best-effort telemetry only. Deliberately NOT common.sh: that resolves a GitHub credential and
# refuses without one, and a deploy must not depend on being able to talk to GitHub.
# shellcheck disable=SC1091
. "$BASE/lib/posthog.sh" 2>/dev/null || posthog_capture() { :; }

# ---------------------------------------------------------------------------- refusals, first

if [ -f "$BASE/state/SYNC_HOLD" ]; then
  log "SYNC_HOLD present — refusing. Remove $BASE/state/SYNC_HOLD to resume."
  exit 0
fi

[ -d "$SRC/.git" ] || { log "FATAL: no git checkout at $SRC"; exit 1; }

# The mirror is a MACHINE checkout, deliberately not the human workspace at
# /home/lem/linkedin_engagement_manager: shipping whatever a half-finished rebase left there is a
# live hazard, and the person doing the rebase would have no idea they had deployed it.
HEAD_SHA="$(git -C "$SRC" rev-parse HEAD 2>/dev/null)"
MAIN_SHA="$(git -C "$SRC" rev-parse origin/main 2>/dev/null)"
[ -n "$HEAD_SHA" ] && [ -n "$MAIN_SHA" ] || { log "FATAL: cannot read $SRC revisions"; exit 1; }

if ! git -C "$SRC" merge-base --is-ancestor "$HEAD_SHA" "$MAIN_SHA"; then
  log "REFUSING: $SRC HEAD ${HEAD_SHA:0:8} is not an ancestor of origin/main ${MAIN_SHA:0:8}."
  exit 1
fi

# ---------------------------------------------------------------------------- is there anything to do

# Hash the pipeline tree rather than trusting the commit: a sync that ran and a commit that only
# touched src/ are the same non-event, and restarting the daemon for the latter costs in-flight runs.
tree_hash() {
  git -C "$SRC" ls-tree -r "$HEAD_SHA" -- scripts/agent-pipeline 2>/dev/null | sha256sum | cut -d' ' -f1
}
NOW_HASH="$(tree_hash)"
LAST_HASH="$(cat "$BASE/state/synced.sha" 2>/dev/null)"
if [ -n "$NOW_HASH" ] && [ "$NOW_HASH" = "$LAST_HASH" ]; then
  exit 0     # quiet: this is the common case, several times an hour
fi

log "pipeline tree changed (${LAST_HASH:0:8}${LAST_HASH:+ }-> ${NOW_HASH:0:8}) at main ${MAIN_SHA:0:8}"

# ---------------------------------------------------------------------------- snapshot, then place

STAMP="$(date +%Y%m%d-%H%M%S)"
SNAP="$SNAPSHOTS/$STAMP"
mkdir -p "$SNAP"
# state/, logs/ and work/ are excluded on purpose: the queue, the ledger and live worktrees are not
# part of the code being rolled back, and restoring a stale queue over a live one would be worse
# than the bad deploy.
for d in lib docs mcp v2; do
  [ -d "$BASE/$d" ] && cp -a "$BASE/$d" "$SNAP/" 2>/dev/null
done
for f in tick.sh status.sh sync.sh RUNBOOK.md .installed.sha256; do
  [ -f "$BASE/$f" ] && cp -a "$BASE/$f" "$SNAP/" 2>/dev/null
done
# shellcheck disable=SC2012
ls -1dt "$SNAPSHOTS"/*/ 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -rf

V2_BEFORE="$(find "$BASE/v2" -name '*.py' -o -name '*.sh' 2>/dev/null | sort | xargs sha256sum 2>/dev/null | sha256sum)"

if ! "$SRC/scripts/agent-pipeline/install.sh" --sync >>"$LOG" 2>&1; then
  # A refusal means a box-edited file. Someone is working on this machine; stop and say so.
  log "install.sh --sync REFUSED — a box-edited file differs from the repo. Nothing restarted."
  log "  read the diff it printed, then either revert the box copy or run --sync --force by hand."
  exit 1
fi

V2_AFTER="$(find "$BASE/v2" -name '*.py' -o -name '*.sh' 2>/dev/null | sort | xargs sha256sum 2>/dev/null | sha256sum)"
printf '%s\n' "$NOW_HASH" > "$BASE/state/synced.sha"

if [ "$V2_BEFORE" = "$V2_AFTER" ]; then
  log "synced; no v2 code changed, so nothing needs restarting."
  exit 0
fi

# A unit file change needs root, which this does not have. Say so rather than half-applying it.
if ! diff -rq "$SNAP/v2/systemd" "$BASE/v2/systemd" >/dev/null 2>&1; then
  log "NOTE: v2/systemd/* changed — a unit file needs 'sudo systemctl daemon-reload' by hand."
fi

# ---------------------------------------------------------------------------- restart, then verify

BEAT_BEFORE="$(cat "$BASE/state/lemd.heartbeat" 2>/dev/null || echo 0)"
for unit in $UNITS; do
  # BOTH units load the `lemd` package. Restarting only the daemon is exactly how the receiver ended
  # up 23 hours stale (#1412), so the list is a variable and both names are in it by default.
  log "restarting $unit"
  sudo -n systemctl restart "$unit" >>"$LOG" 2>&1 || log "WARNING: could not restart $unit"
done

deadline=$(( $(date +%s) + VERIFY_SECONDS ))
healthy=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  sleep 5
  beat="$(cat "$BASE/state/lemd.heartbeat" 2>/dev/null || echo 0)"
  if systemctl is-active --quiet lem-agentd.service && [ "$beat" -gt "$BEAT_BEFORE" ]; then
    healthy=1; break
  fi
done

if [ "$healthy" = "1" ]; then
  log "sync OK — daemon is alive and the heartbeat advanced."
  posthog_capture "lemd_pipeline_synced" "agent-pipeline" \
    "{\"sha\":\"${MAIN_SHA:0:12}\"}" 2>/dev/null || true
  exit 0
fi

# Liveness AND freshness, the same pair the watchdog checks: a unit that is `active` while its
# heartbeat is frozen is the failure this rollback exists for.
log "ROLLING BACK — daemon did not come back within ${VERIFY_SECONDS}s of the sync."
for d in lib docs mcp v2; do
  [ -d "$SNAP/$d" ] && rm -rf "$BASE/$d" && cp -a "$SNAP/$d" "$BASE/" 2>/dev/null
done
for f in tick.sh status.sh sync.sh RUNBOOK.md .installed.sha256; do
  [ -f "$SNAP/$f" ] && cp -a "$SNAP/$f" "$BASE/" 2>/dev/null
done
rm -f "$BASE/state/synced.sha"     # so the next run retries rather than believing it succeeded
for unit in $UNITS; do
  sudo -n systemctl restart "$unit" >>"$LOG" 2>&1 || true
done
log "rolled back to the snapshot at $SNAP."
posthog_capture "lemd_sync_rolled_back" "agent-pipeline" \
  "{\"sha\":\"${MAIN_SHA:0:12}\",\"snapshot\":\"$STAMP\"}" 2>/dev/null || true
exit 1
