#!/usr/bin/env bash
# Run a repo script at the DEPLOYED release tag — the one entry point every `lem` host cron goes
# through (issue #2109). Usage:
#
#   run_at_deployed_tag.sh <repo-relative script> [args...]
#   30 23 * * * /home/lem/cron-runner/repo/scripts/run_at_deployed_tag.sh scripts/perf_snapshot.sh
#
# `scripts/` is not baked into the image, so a host cron executes whatever checkout its crontab
# line names — and those checkouts are ordinary clones nobody pulls. On 2026-09-24 the dev checkout
# two crons ran from sat 31 commits behind `main`, so a fix could merge, ship and still never reach
# the cron that needed it (#2085 was one instance; #2086 pinned only that one sweep).
#
# Every run resolves the tag `scripts/deploy.sh` last converged prod onto (`$LEM_ROOT/.last_good_tag`),
# adds a FRESH detached worktree of it, runs the script from there and removes the worktree on exit.
# The script therefore matches the image it talks to, and the clone's own working tree is never
# reset, pulled or checked out.
#
# It fails CLOSED: an unreadable tag or a tag this clone cannot resolve runs NOTHING and exits 1,
# because running an unknown revision against prod is the defect this exists to end. Each run logs
# the tag + commit that did the work, so a run is attributable after the fact.
#
# The one file this cannot pin is itself — cron executes the clone's copy. It is kept small, and a
# copy that differs from the deployed tag logs a WARNING on every run. Inventory: docs/host-crons.md.
set -uo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/home/lem/.local/bin"

REPO="${CRON_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LEM_ROOT="${LEM_ROOT:-/opt/lem}"
LOG="${CRON_PIN_LOG:-/home/lem/logs/run_at_deployed_tag.log}"
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true

log(){ echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG" >&2; }

if [ $# -lt 1 ]; then
  echo "usage: $(basename "$0") <repo-relative script> [args...]" >&2
  exit 2
fi
REL="$1"; shift

TAG="$(tr -d '[:space:]' < "$LEM_ROOT/.last_good_tag" 2>/dev/null || true)"
# The file is written by deploy.sh, but it is still input: only a release-shaped tag is ever handed
# to git, so a truncated or hand-edited file cannot name a branch or an option.
if ! [[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  log "ERROR: $REL NOT run — deployed tag unreadable from $LEM_ROOT/.last_good_tag (got '${TAG}')"
  exit 1
fi

# A release lands 4x a day, so the tag is usually newer than anything on disk. A failed fetch is not
# fatal on its own — the tag may already be here — but a tag that still does not resolve is.
git -C "$REPO" fetch --quiet --tags origin >>"$LOG" 2>&1 \
  || log "WARNING: git fetch --tags failed in $REPO — resolving $TAG from what is on disk"
SHA="$(git -C "$REPO" rev-parse --verify --quiet "refs/tags/$TAG^{commit}" 2>/dev/null || true)"
if [ -z "$SHA" ]; then
  log "ERROR: $REL NOT run — deployed tag $TAG does not resolve in $REPO"
  exit 1
fi

if ! git -C "$REPO" diff --quiet "$SHA" -- scripts/run_at_deployed_tag.sh 2>/dev/null; then
  log "WARNING: $REPO/scripts/run_at_deployed_tag.sh differs from $TAG — this wrapper cannot pin itself; pull $REPO"
fi

WT="$(mktemp -d "${TMPDIR:-/tmp}/lem-cron-$TAG.XXXXXX")" || { log "ERROR: $REL NOT run — mktemp failed"; exit 1; }
cleanup(){
  git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || rm -rf "$WT"
  git -C "$REPO" worktree prune >/dev/null 2>&1 || true
}
trap cleanup EXIT
if ! git -C "$REPO" worktree add --quiet --detach "$WT" "$SHA" >>"$LOG" 2>&1; then
  log "ERROR: $REL NOT run — could not add a worktree of $TAG in $REPO"
  exit 1
fi
if [ ! -f "$WT/$REL" ]; then
  log "ERROR: $REL NOT run — it does not exist at $TAG"
  exit 1
fi

log "run $REL at $TAG (${SHA:0:8})"
# LEM_DEPLOYED_TAG lets a script pin anything it reads by ref to the same revision it runs from.
# The caller's cwd is kept: worktree_cleanup.sh sweeps the repo it is RUN in, not the one it lives in.
LEM_DEPLOYED_TAG="$TAG" bash "$WT/$REL" "$@"
RC=$?
log "done $REL at $TAG (rc=$RC)"
exit "$RC"
