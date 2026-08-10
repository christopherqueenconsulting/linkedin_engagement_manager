#!/usr/bin/env bash
# Dispatch ONE agent run. The only place v2 spawns `claude -p`.
#
# Usage:  agent_run.sh <mode> <kind> <number> [branch]
#         kind = issue | pr
#
# The daemon chose this item and this mode; everything that could make that choice unsafe is
# re-checked HERE, immediately before the launch, because the daemon's snapshot is by definition
# older than the moment of acting:
#
#   1. PAUSED / human hold — the owner's stop must never lose a race to a scheduling decision.
#   2. Trust walk — re-run against the timeline, fail-closed (see common.sh for why not cached).
#   3. Budget — checked AND charged inside the branch flock, so the check cannot be stale by the
#      time it is acted on, and a crash between the two is impossible. Charged at DISPATCH, so a
#      timeout, a max-turns exhaustion and a pre-commit death all consume a run. That is the whole
#      correction over v1, which counted COMMITS and therefore made the runs that fail early free.
#   4. Branch claim — the same `br-*` flock a v1 tick takes, so the two runners cannot collide
#      during coexistence. The SQLite claim index protects v2 from itself; only the flock protects
#      v2 from v1.
#
# Exit code is the daemon's whole view of what happened, so the codes are distinct on purpose:
# EX_TRUST/EX_BUDGET/EX_BUSY/EX_SETUP never mean "retry this in a minute", and the agent's own rc
# passes through unchanged when a run actually happened.
set -uo pipefail
V2_ACTION="agent_run"
# shellcheck disable=SC1091
. "$(dirname "$0")/common.sh"

MODE="${1:-}"; KIND="${2:-}"; NUMBER="${3:-}"; BRANCH="${4:-}"
[ -n "$MODE" ] && [ -n "$KIND" ] && [ -n "$NUMBER" ] || {
  echo "usage: agent_run.sh <mode> <issue|pr> <number> [branch]" >&2; exit 2; }

# Which label, if any, authorised this lane. Modes absent from the map carry no privilege-granting
# label — `pr_is_upstream` is their boundary (see v2_trust_ok).
case "$MODE" in
  start)   LANE_LABEL="agent:ready" ;;
  revise)  LANE_LABEL="agent:revise" ;;
  depfix)  LANE_LABEL="agent:depfix" ;;
  docfix)  LANE_LABEL="agent:docfix" ;;
  *)       LANE_LABEL="" ;;
esac

if v2_paused; then log "PAUSED — refusing to dispatch $MODE for $KIND #$NUMBER."; exit "$EX_TRUST"; fi
if v2_hold_present "$KIND" "$NUMBER"; then
  log "$KIND #$NUMBER carries a human hold — refusing $MODE."; exit "$EX_TRUST"
fi
if ! v2_trust_ok "$KIND" "$NUMBER" "$LANE_LABEL"; then
  log "TRUST refused $MODE for $KIND #$NUMBER."; exit "$EX_TRUST"
fi

# Resolve the pieces each MODE's prompt needs. A `start` has only an issue; every other mode has a
# PR and may or may not have an issue behind it.
ISSUE=""; PR=""
if [ "$KIND" = "issue" ]; then
  ISSUE="$NUMBER"
  [ -n "$BRANCH" ] || BRANCH="feature/claude-issue-$ISSUE"
else
  PR="$NUMBER"
  ISSUE="$(issue_for_pr "$PR" 2>/dev/null)"
  if [ -z "$BRANCH" ]; then
    BRANCH="$(gh pr view "$PR" --repo "$SLUG" --json headRefName --jq .headRefName 2>/dev/null)"
  fi
fi
[ -n "$BRANCH" ] || { log "no branch resolvable for $KIND #$NUMBER — refusing."; exit "$EX_SETUP"; }

# The branch claim comes BEFORE the budget charge: charging under the lock is what makes
# check-then-charge atomic across both runners, and lib/ledger.sh documents that single-writer
# assumption explicitly.
if ! claim_branch "$BRANCH"; then
  log "branch $BRANCH is claimed by another run — deferring $MODE for $KIND #$NUMBER."
  exit "$EX_BUSY"
fi

# Budget. The reset key must never be derivable from something an agent can produce: agents push
# heads, so keying on the head SHA refills the meter on the agent's own commits (finding H1). `-`
# means "spans the item's lifetime until an owner event resets it".
BUDGET="${LEMD_BUDGET:-4}"
LEDGER_KIND="$KIND"; LEDGER_NUM="$NUMBER"
SPENT="$(ledger_count "$LEDGER_KIND" "$LEDGER_NUM" "$MODE" 2>/dev/null || echo 0)"
if [ "$SPENT" -ge "$BUDGET" ]; then
  log "BUDGET: $KIND #$NUMBER has spent $SPENT/$BUDGET $MODE runs — refusing (the daemon parks)."
  exit "$EX_BUDGET"
fi
ATTEMPTS="$(ledger_charge "$LEDGER_KIND" "$LEDGER_NUM" "$MODE" 2>/dev/null || echo 1)"
log "dispatching $MODE for $KIND #$NUMBER (attempt $ATTEMPTS/$BUDGET, branch $BRANCH)"

if [ "$DRY_RUN" = "1" ]; then
  log "DRY_RUN: would run MODE=$MODE ISSUE=${ISSUE:-} PR=${PR:-} BRANCH=$BRANCH"
  exit 0
fi

# Lane routing needs the capacity preflight to have run: `dispatch_lane` reads CLAUDE_AVAIL /
# OLLAMA_AVAIL / DEGRADED, which only `capacity_preflight` sets. v1 runs it once per tick, so the
# dependency was invisible until v2 called `run_lane` without it — under `set -u` the first live
# dispatch died on an unbound variable AFTER charging the budget. Run it here, in the one action
# that needs it: the gh actions arm merges and park PRs, and neither picks a model.
capacity_preflight 2>/dev/null || true
: "${CLAUDE_AVAIL:=0}" "${OLLAMA_AVAIL:=0}" "${DEGRADED:=0}"
export CLAUDE_AVAIL OLLAMA_AVAIL DEGRADED
# 0 is bash-truth for "available", so these defaults mean "assume both lanes work" when the probe
# itself failed. That is the right direction: the usage-limit pause and the 429 breaker are the real
# gates, and a failed capacity PROBE must not become a silent refusal to work at all.

WT="$(add_worktree "$BRANCH" "origin/main")" || WT=""
if [ -z "$WT" ] || [ ! -d "$WT" ]; then
  log "could not prepare a worktree for $BRANCH — refusing to dispatch."
  exit "$EX_SETUP"
fi

# run_lane.sh reads all of these from the environment.
export MODE ISSUE PR BRANCH WT
export RISK="${RISK:-none}"
# SLOT is compared with `-ge 2` in dispatch.sh to decide the parallel Ollama lane, so it must be a
# NUMBER. Setting it to "v2" (readable, and wrong) made every routing decision emit an
# "integer expression expected" error and fall through to whichever branch bash reached next.
# 1 is the Claude-primary slot, which is the correct default for a single dispatch; the daemon's
# own concurrency is bounded by its pools, not by this.
export SLOT="${SLOT:-1}" WORKER_ID="${WORKER_ID:-v2}"
export EXECUTION_ID="${EXECUTION_ID:-lemd-$$-$(date +%s)}"
export CLAUDE_TIMEOUT="${CLAUDE_TIMEOUT:-45m}"

case "$MODE" in
  start)     PROMPT="Read $RUNBOOK and follow MODE=start. ISSUE=$ISSUE BRANCH=$BRANCH RISK=$RISK WORKTREE=$WT." ;;
  fix)       PROMPT="Read $RUNBOOK and follow MODE=fix. PR=$PR ISSUE=$ISSUE BRANCH=$BRANCH ATTEMPTS=$ATTEMPTS." ;;
  review)    PROMPT="Read $RUNBOOK and follow MODE=review. PR=$PR ISSUE=$ISSUE BRANCH=$BRANCH." ;;
  selfreview) PROMPT="Read $RUNBOOK and follow MODE=selfreview. PR=$PR ISSUE=$ISSUE BRANCH=$BRANCH MARKER='${CLAUDE_REVIEW_MARKER:-🤖 Claude self-review}'." ;;
  rebase)    PROMPT="Read $RUNBOOK and follow MODE=rebase. PR=$PR ISSUE=$ISSUE BRANCH=$BRANCH." ;;
  revise)    PROMPT="Read $RUNBOOK and follow MODE=revise. PR=$PR BRANCH=$BRANCH OWNER=$ASSIGNEE." ;;
  depfix)    PROMPT="Read $RUNBOOK and follow MODE=depfix. PR=$PR BRANCH=$BRANCH." ;;
  docfix)    PROMPT="Read $RUNBOOK and follow MODE=docfix. PR=$PR BRANCH=$BRANCH." ;;
  phasefix)  PROMPT="Read $RUNBOOK and follow MODE=phasefix. PR=$PR ISSUE=$ISSUE BRANCH=$BRANCH." ;;
  *) log "unknown MODE '$MODE' — refusing."; exit 2 ;;
esac

run_lane "$WT" "$PROMPT" "$(model_for_issue "${ISSUE:-}" 2>/dev/null)"
rc=$?
log "$MODE for $KIND #$NUMBER finished rc=$rc"

# Release the worktree the same way v1's EXIT trap does — release_worktree refuses to remove one
# holding unpushed work, which is the guard that matters when a run is killed mid-commit.
release_worktree "$WT" >/dev/null 2>&1 || true
exit "$rc"
