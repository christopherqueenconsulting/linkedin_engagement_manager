#!/usr/bin/env bash
# Agent pipeline driver — one "advance the pipeline by one step" tick.
# Serial (cap=1): finishes the in-flight agent PR before starting a new issue.
# Uses the owner's Claude Max login (no API cost). Never touches the dev checkout's branch —
# all issue work happens in isolated git worktrees. Deploy happens via the existing
# merge->release-please->build->deploy pipeline once a PR lands on main.
#
# MERGE IS RUNNER-CONTROLLED (not GitHub eager auto-merge): a PR is merged ONLY after
#   (1) all CI checks are green, (2) Copilot has reviewed the current head commit, and
#   (3) there are zero unresolved Copilot review threads.
# This guarantees Copilot's comments are read + addressed BEFORE merge.
set -uo pipefail

# --- config ---
BASE="/home/lem/agent-pipeline"
REPO="/home/lem/linkedin_engagement_manager"
SLUG="christopherqueenconsulting/linkedin_engagement_manager"
OWNER="${SLUG%%/*}"; NAME="${SLUG##*/}"
ASSIGNEE="gitchrisqueen"
COPILOT="copilot-pull-request-reviewer"
WORKROOT="$BASE/work"
LOGDIR="$BASE/logs"
RUNBOOK="$BASE/RUNBOOK.md"
PAUSED="$BASE/PAUSED"
LOCK="$BASE/lock"
MAX_FIX_ATTEMPTS=4
CLAUDE_TIMEOUT="45m"
DRY_RUN="${DRY_RUN:-0}"

export PATH="/home/lem/.local/bin:/usr/local/bin:/usr/bin:/bin"
export HOME="/home/lem"
export GH_PROMPT_DISABLED=1

mkdir -p "$WORKROOT" "$LOGDIR"
LOG="$LOGDIR/tick-$(date +%Y%m%d).log"
log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG" ; }

# --- guards ---
# DRY_RUN is inert (no pushes, no Claude, no merges) so it may validate even while paused.
if [ -f "$PAUSED" ] && [ "$DRY_RUN" != "1" ]; then log "PAUSED file present — skipping."; exit 0; fi
exec 9>"$LOCK"
flock -n 9 || { log "another tick holds the lock — skipping."; exit 0; }

# --- helpers ---
epoch() { date -d "$1" +%s 2>/dev/null || echo 0; }

select_next_issue() {
  # Next ready issue: has agent:ready, not needs-human/agent:working/agent:blocked,
  # ordered by milestone number (7->12) then priority then issue number.
  gh issue list --repo "$SLUG" --state open --limit 100 --label "agent:ready" \
    --json number,labels,milestone | jq -r '
    def prio: (.labels|map(.name)|map(select(startswith("priority:")))|.[0] // "priority:zzz")
      | {"priority:critical":0,"priority:high":1,"priority:medium":2,"priority:low":3}[.] // 4;
    def msnum: (.milestone.title // "Milestone 999" | capture("Milestone (?<n>[0-9]+)").n | tonumber);
    map(select((.labels|map(.name)) as $l
        | ($l|index("needs-human")|not) and ($l|index("agent:blocked")|not) and ($l|index("agent:working")|not)))
    | sort_by(msnum, prio, .number)
    | .[0].number // empty'
}

open_agent_pr() {
  gh pr list --repo "$SLUG" --state open --label "agent:working" \
    --json number,headRefName,labels | jq -r '.[0] // empty | @json'
}

# Count of UNRESOLVED review threads whose first comment is authored by Copilot.
copilot_unresolved_threads() {  # $1=pr
  gh api graphql -f query='
    query($owner:String!,$name:String!,$pr:Int!){
      repository(owner:$owner,name:$name){ pullRequest(number:$pr){
        reviewThreads(first:100){ nodes{ isResolved comments(first:1){ nodes{ author{ login } } } } } } } }' \
    -f owner="$OWNER" -f name="$NAME" -F pr="$1" 2>/dev/null \
  | jq --arg c "$COPILOT" '[.data.repository.pullRequest.reviewThreads.nodes[]?
       | select(.isResolved==false)
       | select((.comments.nodes[0].author.login // "")|test($c;"i"))] | length' 2>/dev/null || echo 0
}

# ISO timestamp of Copilot's most recent submitted review (empty if none).
copilot_last_review_at() {  # $1=pr
  gh pr view "$1" --repo "$SLUG" --json reviews \
    | jq -r --arg c "$COPILOT" '[.reviews[]?|select(.author.login==$c)]|last|.submittedAt // empty' 2>/dev/null
}

add_worktree() {  # $1=branch  $2=base(ref)  -> path on stdout
  local branch="$1" base="$2" wt="$WORKROOT/$1"
  git -C "$REPO" worktree prune >/dev/null 2>&1
  rm -rf "$wt"
  if git -C "$REPO" show-ref --verify --quiet "refs/remotes/origin/$branch"; then
    git -C "$REPO" worktree add "$wt" "origin/$branch" >/dev/null 2>&1
    git -C "$wt" checkout -B "$branch" "origin/$branch" >/dev/null 2>&1
  else
    git -C "$REPO" worktree add -b "$branch" "$wt" "$base" >/dev/null 2>&1
  fi
  echo "$wt"
}

run_claude() {  # $1=worktree  $2=prompt
  local wt="$1" prompt="$2"
  if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: would run claude in $wt"; return 0; fi
  ( cd "$wt" && timeout "$CLAUDE_TIMEOUT" claude -p "$prompt" \
      --dangerously-skip-permissions --add-dir "$BASE" ) >>"$LOG" 2>&1
  local rc=$?
  [ $rc -ne 0 ] && log "claude exited rc=$rc (timeout/interrupt/limit) — will retry next tick."
  return $rc
}

# --- state machine ---
git -C "$REPO" fetch origin --prune >/dev/null 2>&1

PR_JSON="$(open_agent_pr)"

if [ -n "$PR_JSON" ]; then
  PR="$(echo "$PR_JSON" | jq -r .number)"
  BRANCH="$(echo "$PR_JSON" | jq -r .headRefName)"
  ISSUE="$(gh pr view "$PR" --repo "$SLUG" --json body,title \
           | jq -r '(.body,.title)|scan("#([0-9]{3,})")|.[0]' | head -1)"
  HEAD_DATE="$(git -C "$REPO" log -1 --format=%cI "origin/$BRANCH" 2>/dev/null)"
  log "In-flight PR #$PR (branch $BRANCH, issue #${ISSUE:-?})."

  ROLLUP="$(gh pr view "$PR" --repo "$SLUG" --json statusCheckRollup \
    | jq -r '[.statusCheckRollup[]? | {s:(.conclusion//.state//"PENDING")}]')"
  FAILED="$(echo "$ROLLUP" | jq '[.[]|select(.s=="FAILURE" or .s=="ERROR" or .s=="TIMED_OUT" or .s=="CANCELLED")]|length')"
  PENDING="$(echo "$ROLLUP" | jq '[.[]|select(.s=="PENDING" or .s=="QUEUED" or .s=="IN_PROGRESS" or .s=="EXPECTED")]|length')"
  ATTEMPTS="$(git -C "$REPO" rev-list --count "origin/main..origin/$BRANCH" 2>/dev/null || echo 1)"
  UNRESOLVED="$(copilot_unresolved_threads "$PR")"
  CP_AT="$(copilot_last_review_at "$PR")"

  # 1) CI failing -> fix (or escalate after too many tries)
  if [ "${FAILED:-0}" -gt 0 ]; then
    if [ "${ATTEMPTS:-1}" -ge "$MAX_FIX_ATTEMPTS" ]; then
      log "PR #$PR failing after $ATTEMPTS attempts — escalating to human."
      if [ "$DRY_RUN" != "1" ]; then
        gh pr edit "$PR" --repo "$SLUG" --add-label needs-human --add-label agent:blocked --remove-label agent:working >/dev/null 2>&1
        gh pr ready --undo "$PR" --repo "$SLUG" >/dev/null 2>&1
        [ -n "$ISSUE" ] && gh issue edit "$ISSUE" --repo "$SLUG" --add-label needs-human --add-label agent:blocked --add-assignee "$ASSIGNEE" --remove-label agent:working >/dev/null 2>&1
        gh pr comment "$PR" --repo "$SLUG" --body "🚧 Auto-fix gave up after $ATTEMPTS attempts. Assigning @$ASSIGNEE — CI is still red." >/dev/null 2>&1
      fi
      exit 0
    fi
    log "PR #$PR CI failing (attempt $ATTEMPTS) — invoking fix."
    WT="$(add_worktree "$BRANCH" origin/main)"
    export MODE=fix PR ISSUE WORKTREE="$WT" BRANCH ATTEMPTS
    run_claude "$WT" "Read $RUNBOOK and follow MODE=fix. PR=$PR ISSUE=$ISSUE BRANCH=$BRANCH ATTEMPTS=$ATTEMPTS."
    exit 0
  fi

  # 2) Copilot has unresolved review threads -> address + resolve them BEFORE any merge
  if [ "${UNRESOLVED:-0}" -gt 0 ]; then
    log "PR #$PR — $UNRESOLVED unresolved Copilot thread(s) — invoking review-address."
    WT="$(add_worktree "$BRANCH" origin/main)"
    export MODE=review PR ISSUE WORKTREE="$WT" BRANCH
    run_claude "$WT" "Read $RUNBOOK and follow MODE=review. PR=$PR ISSUE=$ISSUE BRANCH=$BRANCH."
    exit 0
  fi

  # 3) CI still running -> wait
  if [ "${PENDING:-0}" -gt 0 ]; then
    log "PR #$PR — CI still running ($PENDING pending). Waiting."
    exit 0
  fi

  # 4) CI green + no unresolved Copilot threads. Require Copilot to have reviewed the CURRENT head.
  if [ -z "$CP_AT" ] || [ "$(epoch "$CP_AT")" -lt "$(epoch "$HEAD_DATE")" ]; then
    log "PR #$PR — green, but waiting for Copilot to review the current head commit before merge."
    exit 0
  fi

  # 5) Green + Copilot reviewed head + all threads resolved -> merge (runner-controlled).
  log "PR #$PR — green, Copilot reviewed & all threads resolved. Merging."
  if [ "$DRY_RUN" != "1" ]; then
    gh pr merge --auto "$(gh pr view "$PR" --repo "$SLUG" --json url --jq .url)" >/dev/null 2>&1 \
      && gh pr comment "$PR" --repo "$SLUG" --body "✅ CI green, Copilot review addressed & resolved — merging." >/dev/null 2>&1
  fi
  exit 0
fi

# No in-flight PR — start the next ready issue.
ISSUE="$(select_next_issue)"
if [ -z "$ISSUE" ]; then
  log "No agent:ready issues remaining. Pipeline idle."
  exit 0
fi
RISK="$(gh issue view "$ISSUE" --repo "$SLUG" --json labels \
        | jq -r '[.labels[].name|select(startswith("risk:"))|sub("risk:";"")]|join(" ")')"
[ -z "$RISK" ] && RISK="none"
BRANCH="feature/claude-issue-$ISSUE"
log "Starting issue #$ISSUE (risk=$RISK) on $BRANCH."
if [ "$DRY_RUN" = "1" ]; then
  log "DRY_RUN: would create worktree $BRANCH and run MODE=start for #$ISSUE."
  exit 0
fi
gh issue edit "$ISSUE" --repo "$SLUG" --add-label agent:working --remove-label agent:ready >/dev/null 2>&1
WT="$(add_worktree "$BRANCH" origin/main)"
export MODE=start ISSUE WORKTREE="$WT" BRANCH RISK
run_claude "$WT" "Read $RUNBOOK and follow MODE=start. ISSUE=$ISSUE BRANCH=$BRANCH RISK=$RISK WORKTREE=$WT."
exit 0
