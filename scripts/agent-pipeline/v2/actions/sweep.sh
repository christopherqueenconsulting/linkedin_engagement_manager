#!/usr/bin/env bash
# Sweep stale worktrees under $WORKROOT — the v2 call site of `sweep_stale_worktrees` (#2041).
#
# Usage: sweep.sh            (no arguments; the daemon spawns it on its own clock)
#
# The sweep had exactly one caller, `tick.sh`, and `tick.sh` exits at the top once `V1_RETIRED`
# exists. So from the 2026-08-10 cutover nothing swept `work/` at all: 76 worktrees and 8.7 GB by
# 2026-09-11, most of them clean trees whose PR had squash-merged weeks earlier. A call site that
# lives only inside a work path dies with that path; this one is owned by the daemon LOOP
# (`Daemon.sweep_worktrees`), so it runs whether or not any item is being worked.
#
# Two rate limits, deliberately the SAME stamp file: `sweep_stale_worktrees` keeps its own hourly
# guard (`locks/.worktree-sweep`, `WORKTREE_SWEEP_INTERVAL`) so a v1 failsafe tick and this action
# can never both stat every worktree in the same hour, and the daemon reads that stamp BEFORE
# spawning so it does not start a process an hour early that the guard would only send home.
#
# Nothing here decides what is removable. `release_worktree` refuses a dirty tree, a claimed branch
# and anything outside `$WORKROOT`; `sweep_stale_worktrees` adds the GitHub open/merged evidence and
# the two-hour grace. This script only makes sure the function is REACHED, and says so in the log,
# because "the sweep is wired" is exactly the claim that turned out to be false.
set -uo pipefail
V2_ACTION="sweep"
# shellcheck disable=SC1091
. "$(dirname "$0")/common.sh"

# common.sh refuses to source without lib/guards.sh, but a guards.sh that lost the function would
# still source cleanly — and a 127 from an undefined function is the silent death this fixes.
if ! command -v sweep_stale_worktrees >/dev/null 2>&1; then
  log "FATAL: sweep_stale_worktrees is not defined by lib/guards.sh — refusing."
  exit "$EX_SETUP"
fi

if [ "$DRY_RUN" = "1" ]; then
  log "DRY_RUN: would sweep stale worktrees under $WORKROOT."
  exit 0
fi

count_registered() { git -C "$REPO" worktree list --porcelain 2>/dev/null | grep -c '^worktree ' || true; }

STAMP="$BASE/locks/.worktree-sweep"
before_stamp="$(stat -c %Y "$STAMP" 2>/dev/null || echo 0)"
before_count="$(count_registered)"

sweep_stale_worktrees

# The function touches its stamp only when it actually walks the worktrees; an untouched stamp means
# its hourly guard sent it home. Reported in different words so an operator grepping for
# `worktree sweep:` counts sweeps, never skips.
after_stamp="$(stat -c %Y "$STAMP" 2>/dev/null || echo 0)"
if [ "$after_stamp" = "$before_stamp" ]; then
  log "stale-worktree sweep skipped: last ran $(( $(date +%s) - before_stamp ))s ago (WORKTREE_SWEEP_INTERVAL=${WORKTREE_SWEEP_INTERVAL:-3600})."
  exit 0
fi
after_count="$(count_registered)"
log "worktree sweep: complete — $before_count registered before, $after_count after, $(( before_count - after_count )) removed under $WORKROOT."
exit 0
