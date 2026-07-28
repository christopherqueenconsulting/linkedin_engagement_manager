#!/usr/bin/env bash
# Agent pipeline driver — one "advance the pipeline by one step" tick.
# Concurrency scales with the backlog (slots = 1 + ready/10, capped — see config.env); per-branch
# claims keep slots off each other's work, and a WIP gate stops new issues outrunning merges.
# Uses the owner's Claude Max login (no API cost). Never touches the dev checkout's branch —
# all issue work happens in isolated git worktrees. Deploy happens via the existing
# merge->release-please->build->deploy pipeline once a PR lands on main.
#
# MERGE IS RUNNER-CONTROLLED (not GitHub eager auto-merge): a PR is merged ONLY after
#   (1) all CI checks are green, (2) ONE fresh review exists — the Claude adversarial review
#       marker (default reviewer, MODE=selfreview) or a Copilot review (selective: only PRs
#       labeled risk:*/review:copilot get Copilot credits spent on them), and
#   (3) there are zero unresolved Copilot review threads (when Copilot did review).
# This guarantees every merge was reviewed, without metered per-push Copilot spend.
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

# Owner-tunable knobs (edit $BASE/config.env; missing file = these defaults).
#   MAX_AGENTS         hard ceiling on concurrent Claude runs (slots), whatever the backlog
#   SCALE_PER_ISSUES   +1 slot per this many agent:ready issues (1 + N/SCALE, capped)
#   BUSY_HOURS_UTC     e.g. "13-23": during these UTC hours force 1 slot so pipeline agents
#                      don't compete with the owner's interactive Claude usage on other projects
#   USAGE_PAUSE_MINUTES how long to self-pause when a run hits a usage/rate limit
#   PHASE_GUARD        1 (default) = hold a merge that would close an issue with a declared,
#                      untracked later phase; 0 = off (see "Phase guard" below)
[ -f "$BASE/config.env" ] && . "$BASE/config.env"
MAX_AGENTS="${MAX_AGENTS:-3}"
SCALE_PER_ISSUES="${SCALE_PER_ISSUES:-10}"
BUSY_HOURS_UTC="${BUSY_HOURS_UTC:-}"
USAGE_PAUSE_MINUTES="${USAGE_PAUSE_MINUTES:-60}"

export PATH="/home/lem/.local/bin:/usr/local/bin:/usr/bin:/bin"
export HOME="/home/lem"
export GH_PROMPT_DISABLED=1

mkdir -p "$WORKROOT" "$LOGDIR" "$BASE/locks"
LOG="$LOGDIR/tick-$(date +%Y%m%d).log"
log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG" ; }

# --- capacity-aware routing (Claude subscription primary, Ollama cloud via LiteLLM fallback +
#     parallel lane). Libs live in $BASE/lib; they reuse the existing PostHog keys from secrets.env.
#     Preflight runs ONCE per tick so the CAP/degraded logic below can react before any run;
#     dispatch_lane() (called inside run_claude -> run_lane) picks the lane per run. See
#     docs/agent-pipeline-routing.md for the >50% rule and the lane model. ---
_TICK_LOG="$LOG"
EXECUTION_ID="tick-$$-$(date +%s)"
export BASE _TICK_LOG EXECUTION_ID REPO SLUG
# shellcheck disable=SC1091
for _l in posthog labels capacity dispatch run_lane; do . "$BASE/lib/$_l.sh" 2>/dev/null || true; done
ensure_ai_labels 2>/dev/null || true   # idempotent bootstrap of the ai:* labels (first tick only)
capacity_preflight 2>/dev/null || true  # sets CLAUDE_PCT/OLLAMA_PCT/CLAUDE_AVAIL/OLLAMA_AVAIL/DEGRADED

# --- guards ---
# DRY_RUN is inert (no pushes, no Claude, no merges) so it may validate even while paused.
if [ -f "$PAUSED" ] && [ "$DRY_RUN" != "1" ]; then log "PAUSED file present — skipping."; exit 0; fi
# Usage-limit pause is now LANE-SPECIFIC (CLAUDE_PAUSED_UNTIL / OLLAMA_PAUSED_UNTIL, written by
# run_lane and the probe in capacity.sh) and gated per-lane inside capacity_preflight + dispatch_lane
# — a Claude usage-limit no longer idles the Ollama lane, and the probe loop resumes Claude the
# moment usage resets. Clean up any stray pre-split whole-pipeline PAUSED_UNTIL so an old file can't
# confuse a reader; it no longer skips the tick.
[ -f "$BASE/PAUSED_UNTIL" ] && { rm -f "$BASE/PAUSED_UNTIL"; log "removed stray whole-pipeline PAUSED_UNTIL (now lane-specific)."; }

# BOTH lanes known-exhausted: don't waste a spin. capacity_preflight already ran the Claude probe
# this tick (it will resume Claude the moment a post-reset probe succeeds); Ollama recovers via its
# own pause expiry + the liveliness probe. There is nothing productive to dispatch until one comes
# back, so idle this tick — cron keeps firing every 5 min, so recovery is picked up on the next one.
case "${CLAUDE_STATUS:-}:${OLLAMA_STATUS:-}" in
  exhausted:exhausted|exhausted:unavailable|exhausted:disabled)
    log "both lanes exhausted/unavailable (claude=${CLAUDE_STATUS} ollama=${OLLAMA_STATUS}) — idling until a probe/liveliness recovers one."
    exit 0 ;;
esac

# --- concurrency: slots scale with the backlog (1 + ready/SCALE_PER_ISSUES, capped) ---
# Each tick process occupies ONE slot for its lifetime; cron keeps firing every 5 min, so when
# CAP > 1 the next tick runs alongside instead of skipping. Per-branch claims (claim_branch)
# keep two slots off the same PR/issue. During BUSY_HOURS_UTC the cap is forced to 1 so agents
# yield the subscription's usage window to the owner's interactive work.
READY_COUNT="$(gh issue list --repo "$SLUG" --state open --limit 100 --label "agent:ready" \
                 --json number --jq 'length' 2>/dev/null || echo 0)"
CAP=$(( 1 + READY_COUNT / SCALE_PER_ISSUES ))
[ "$CAP" -gt "$MAX_AGENTS" ] && CAP="$MAX_AGENTS"
if [ -n "$BUSY_HOURS_UTC" ]; then
  H=$((10#$(date -u +%H))); B_START="${BUSY_HOURS_UTC%-*}"; B_END="${BUSY_HOURS_UTC#*-}"
  if { [ "$B_START" -le "$B_END" ] && [ "$H" -ge "$B_START" ] && [ "$H" -lt "$B_END" ]; } \
     || { [ "$B_START" -gt "$B_END" ] && { [ "$H" -ge "$B_START" ] || [ "$H" -lt "$B_END" ]; }; }; then
    CAP=1
  fi
fi
# Degraded mode (both lanes <=50% capacity, set by capacity_preflight): force low concurrency so
# the pipeline doesn't fan out expensive work onto constrained providers. Merge/fix/review/answer
# lanes above still run; only NEW issue starts are held (see the START section).
if [ "${DEGRADED:-0}" = "1" ]; then
  CAP=1
  log "DEGRADED: both lanes constrained (claude=${CLAUDE_PCT:-?}% ollama=${OLLAMA_PCT:-?}%) — CAP forced to 1, new starts held."
fi
SLOT=""
for _s in $(seq 1 "$CAP"); do
  eval "exec $((10 + _s))>\"$BASE/locks/slot-${_s}.lock\""
  if flock -n $((10 + _s)); then SLOT="$_s"; break; fi
done
if [ -z "$SLOT" ]; then log "all $CAP agent slot(s) busy — skipping."; exit 0; fi
[ "$CAP" -gt 1 ] && log "slot $SLOT/$CAP (ready issues: $READY_COUNT)"

# Per-branch work claim so concurrent slots never touch the same PR/issue. Re-opening fd 10
# releases any previously held claim (fd close drops the flock).
claim_branch() {  # $1=branch -> 0 claimed, 1 busy
  exec 10>"$BASE/locks/br-$(echo "$1" | tr '/' '_').lock"
  flock -n 10
}

# --- helpers ---
epoch() { date -d "$1" +%s 2>/dev/null || echo 0; }

newest_owner_answer() {
  # newest_owner_answer pr|issue <number> -> the owner's newest reply to the LATEST Decision Comment
  # on that thread (empty if none). Shared by the PR and ISSUE answer lanes so they can't drift.
  # Decision Comments and agent replies are authored under the OWNER'S login (agents post with the
  # owner's gh token), so they're excluded by BODY SIGNATURE, not author. Only comments AFTER the
  # latest Decision Comment count — else re-parking would instantly re-route on a stale answer.
  # Non-owner comments are skipped rather than ending the search, so a bot commenting after the
  # owner can't bury the answer.
  gh "$1" view "$2" --repo "$SLUG" --json comments 2>/dev/null \
    | jq -r --arg owner "$ASSIGNEE" '
        def isdecision: ((.body // "") | test("Human decision needed"; "i"));
        def isagent:    ((.body // "") | test("Generated with \\[Claude Code\\]"));
        ((.comments // []) | to_entries) as $c
        | (($c | map(select(.value | isdecision)) | last | .key) // -1) as $di
        | [ $c[] | select(.key > $di) | .value
            | select((.author.login // "") == $owner)
            | select(isdecision | not) | select(isagent | not) ]
        | (last // empty) | (.body // "") | @base64' 2>/dev/null
}

answer_verdict() {
  # answer_verdict "<comment body>" -> answer | directive | hold | question | (empty = not a decision)
  # Shape is judged on the FIRST non-empty line; MODE=revise/start then read the WHOLE comment, so
  # trailing context, off-menu options and side-instructions all reach the agent verbatim.
  local LB="$1" FIRST SHAPE
  FIRST="$(printf '%s\n' "$LB" | awk '{sub(/\r$/,"")} NF{print; exit}')"
  [ -n "$FIRST" ] && [ ${#LB} -lt 8000 ] || return 0
  SHAPE=""
  # The [^[:alnum:]] after the letter stops "2 things I want changed" reading as option 2T.
  if echo "$FIRST" | grep -qiE '^[[:space:]]*(ok\b|okay\b|[0-9]+[[:space:]]*[A-Za-z]([^[:alnum:]]|$))'; then
    SHAPE="answer"
  elif echo "$FIRST" | grep -qiE '^[[:space:]]*(@claude\b|decision:|go:)'; then
    SHAPE="directive"
  fi
  [ -n "$SHAPE" ] || return 0
  # Ambiguity fails toward the human: an explicit hold wins even when the reply leads with tokens,
  # and a free-form reply that reads as a question is a question, not a decision.
  if echo "$LB" | grep -qiE "(don'?t|do not) (merge|land|ship|start)|hold (off|on|this|it)\b|\bon hold\b|\bnot yet\b|wait (for|on|until|till)\b|stand ?by\b"; then
    echo "hold"; return 0
  fi
  if [ "$SHAPE" = "directive" ] && echo "$LB" | grep -qE '\?[[:space:]]*$'; then
    echo "question"; return 0
  fi
  echo "$SHAPE"
}

issue_for_pr() {
  # Issue that PR $1 closes. Uses GitHub's OWN development link, NOT a "#N" scan of the PR body —
  # that scan returns the FIRST number mentioned, so a PR citing prior work ("the lane #553 added")
  # reported the wrong issue and handed a wrong ISSUE to the fix/review/rebase prompts.
  local P="$1" I
  I="$(gh pr view "$P" --repo "$SLUG" --json closingIssuesReferences 2>/dev/null \
       | jq -r '[(.closingIssuesReferences // [])[] | .number] | (first // empty)')"
  [ -n "$I" ] && { echo "$I"; return 0; }
  # Fallbacks: the agent branch convention, then a "closes/fixes/resolves #N" keyword in the body.
  gh pr view "$P" --repo "$SLUG" --json headRefName,body 2>/dev/null | jq -r '
      (.headRefName // "") as $b
      | if ($b | test("^feature/claude-issue-[0-9]+$")) then ($b | capture("(?<n>[0-9]+)$").n)
        else (((.body // "") | capture("(?i)(clos(e|es|ed)|fix(e[sd])?|resolv(e|es|ed))[[:space:]]+#(?<n>[0-9]+)").n) // empty)
        end'
}

pr_for_issue() {
  # Open PR belonging to issue $1. Uses GitHub's OWN linkage (the "Closes #N" development link) —
  # NOT a "#N" scan of PR bodies, which false-matches any PR that merely cites the issue as context
  # (e.g. #616's body cites "#403/#404", so a body scan wrongly claimed #616's PR owns #404).
  # Falls back to the agent branch naming convention for a PR that forgot the closing keyword.
  local N="$1" P
  P="$(gh issue view "$N" --repo "$SLUG" --json closedByPullRequestsReferences 2>/dev/null \
       | jq -r '[(.closedByPullRequestsReferences // [])[] | .number] | (first // empty)')"
  if [ -n "$P" ]; then
    # Only route work that's still open.
    [ "$(gh pr view "$P" --repo "$SLUG" --json state --jq .state 2>/dev/null)" = "OPEN" ] && { echo "$P"; return 0; }
    return 0
  fi
  gh pr list --repo "$SLUG" --state open --limit 50 --json number,headRefName 2>/dev/null \
    | jq -r --arg n "$N" '[ .[] | select(.headRefName == "feature/claude-issue-" + $n) | .number ] | (first // empty)'
}

select_next_issue() {
  # Next ready issue: has agent:ready, not needs-human/agent:working/agent:blocked,
  # ordered: critical/high priority JUMP the line (regardless of milestone), then everyone
  # else by milestone number (7->12), then priority, then issue number.
  gh issue list --repo "$SLUG" --state open --limit 100 --label "agent:ready" \
    --json number,labels,milestone | jq -r '
    def prio: (.labels|map(.name)|map(select(startswith("priority:")))|.[0] // "priority:zzz")
      | {"priority:critical":0,"priority:high":1,"priority:medium":2,"priority:low":3}[.] // 4;
    def msnum: (.milestone.title // "Milestone 999" | capture("Milestone (?<n>[0-9]+)").n | tonumber);
    map(select((.labels|map(.name)) as $l
        | ($l|index("needs-human")|not) and ($l|index("agent:blocked")|not) and ($l|index("agent:working")|not)))
    | sort_by((if prio <= 1 then 0 else 1 end), msnum, prio, .number)
    | .[0].number // empty'
}

open_agent_pr() {
  # Prefer a CONFLICTING (DIRTY) PR — it needs a rebase and blocks its own merge — then by PR number.
  gh pr list --repo "$SLUG" --state open --label "agent:working" \
    --json number,headRefName,labels,mergeStateStatus \
    | jq -r 'sort_by((if .mergeStateStatus=="DIRTY" then 0 else 1 end), .number) | .[0] // empty | @json'
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

# --- Review economy (2026-07-26) ---
# Copilot code review is metered in AI credits and one month of per-push reviews burned ~7,500
# credits (~$60 overage) — per-PR-per-push Copilot doesn't fit any plan at pipeline velocity, and
# when the quota dies the old gate deadlocked (request API 200s but requested_reviewers stays
# empty). The economy now:
#   * DEFAULT gate reviewer: a Claude ADVERSARIAL review (MODE=selfreview) on the owner's flat-rate
#     Max login — fresh context, finds AND fixes, then posts a marker comment the gate accepts.
#   * Copilot: SELECTIVE second opinion — requested once, only after CI is green, and only on PRs
#     labeled risk:* or review:copilot. If Copilot doesn't deliver within
#     FIRST_REVIEW_TIMEOUT_SECONDS, fall back per REVIEW_FALLBACK:
#       claude (default) = run the adversarial review instead;  merge = merge with a ⚠️ comment;
#       hold = keep waiting.
CLAUDE_REVIEW_MARKER="🔎 Claude adversarial review"
FIRST_REVIEW_TIMEOUT_SECONDS="${FIRST_REVIEW_TIMEOUT_SECONDS:-3600}"
REVIEW_FALLBACK="${REVIEW_FALLBACK:-claude}"

copilot_wanted() {  # $1=pr -> 0 when this PR merits spending Copilot credits
  gh pr view "$1" --repo "$SLUG" --json labels 2>/dev/null \
    | jq -e '[.labels[].name | select(startswith("risk:") or . == "review:copilot")] | length > 0' >/dev/null 2>&1
}

copilot_request_pending() {  # $1=pr -> 0 when a Copilot review request is already queued
  gh pr view "$1" --repo "$SLUG" --json reviewRequests 2>/dev/null \
    | jq -e '(.reviewRequests // []) | tostring | test("copilot"; "i")' >/dev/null 2>&1
}

try_request_copilot_review() {  # $1=pr (best-effort, silent; skips when already pending or already submitted)
  copilot_request_pending "$1" && return 0
  [ -n "$(copilot_last_review_at "$1")" ] && return 0
  gh api -X POST "repos/$SLUG/pulls/$1/requested_reviewers" \
    -f 'reviewers[]=copilot-pull-request-reviewer[bot]' >/dev/null 2>&1 || true
}

# ISO timestamp of the newest Claude adversarial-review marker comment (empty if none).
claude_reviewed_at() {  # $1=pr
  gh pr view "$1" --repo "$SLUG" --json comments 2>/dev/null \
    | jq -r --arg m "$CLAUDE_REVIEW_MARKER" \
        '[(.comments // [])[] | select((.body // "") | startswith($m))] | last | .createdAt // empty' 2>/dev/null
}

# 0 when a Claude adversarial-review marker exists that is fresh for the current head.
claude_marker_fresh_p() {  # $1=pr $2=head-commit-iso-date
  local at="$(claude_reviewed_at "$1")"
  [ -n "$at" ] || return 1
  [ -n "$2" ] || return 0          # no head date -> treat existing marker as acceptable
  [ "$(epoch "$at")" -ge "$(epoch "$2")" ] && return 0
  [ "$(( $(date +%s) - $(epoch "$2") ))" -ge "$REVIEW_GRACE_SECONDS" ] && return 0
  return 1
}

# 0 when the auto-fix escalation comment has already been posted on this PR.
auto_fix_gave_up_p() {  # $1=pr
  gh pr view "$1" --repo "$SLUG" --json comments 2>/dev/null \
    | jq -r '((.comments // [])[].body // "")' 2>/dev/null \
    | grep -qF "🚧 Auto-fix gave up"
}

# Freshest acceptable review timestamp for the merge gate: Copilot's or Claude's, whichever is newer.
best_review_at() {  # $1=pr
  local a b
  a="$(copilot_last_review_at "$1")"
  b="$(claude_reviewed_at "$1")"
  if [ "$(epoch "${a:-0}")" -ge "$(epoch "${b:-0}")" ]; then echo "$a"; else echo "$b"; fi
}

review_wait_expired() {  # $1=head-commit-iso-date -> 0 when the no-review fallback window passed
  [ -n "$1" ] || return 1
  [ "$(( $(date +%s) - $(epoch "$1") ))" -ge "$FIRST_REVIEW_TIMEOUT_SECONDS" ]
}

# --- Phase guard (2026-07-27) — a merge must never close an issue that still has scope left ---
# #548 shipped "Phase 1" and its PR auto-closed the issue; Phase 2 was never filed and the work
# silently vanished (same for #568, #647). Before ANY merge, read the issue the PR closes:
#   * an EXPLICIT later phase ("Phase 2", "lands in a follow-up PR", "deferred to", …) with no
#     linked follow-up  -> park the PR + escalate the issue, never merge it silently;
#   * merely UNCHECKED acceptance boxes -> one warning comment, merge proceeds (acceptance lists
#     in this repo are routinely left unticked, so parking on those alone would stall everything).
# A "#N" sitting next to follow-up/phase wording — on the PR or in the issue's comments — counts as
# the follow-up and clears the guard. FAIL-OPEN: any gh/jq hiccup returns "safe to merge", because
# a broken check must never wedge the pipeline.
PHASE_GUARD="${PHASE_GUARD:-1}"
PHASE_GUARD_MARKER="🧩 phase-guard"

phase_leftover() {  # $1=issue -> echoes "phase: <marker>" / "boxes: <n>" / nothing
  local N="$1" body mark boxes
  body="$(gh issue view "$N" --repo "$SLUG" --json body --jq '.body // ""' 2>/dev/null)" || return 0
  [ -n "$body" ] || return 0
  mark="$(printf '%s' "$body" | grep -oiE 'phase [2-9]|part [2-9]|next phase|later phase|follow-up (pr|issue)|land[s]? in a follow-up|deferred to|out of scope for this issue|will be handled in|tracked separately' | head -1)"
  [ -n "$mark" ] && { echo "phase: \"$mark\""; return 0; }
  boxes="$(printf '%s' "$body" | grep -cE '^[[:space:]]*[-*][[:space:]]+\[[[:space:]]\]')"
  [ "${boxes:-0}" -gt 0 ] && echo "boxes: $boxes unchecked"
  return 0
}

phase_followup_linked() {  # $1=pr $2=issue -> 0 when a follow-up issue is already linked
  local P="$1" N="$2" hit
  hit="$( { gh pr view "$P" --repo "$SLUG" --json body,comments --jq '.body, ((.comments // [])[].body)' 2>/dev/null
            gh issue view "$N" --repo "$SLUG" --json comments --jq '((.comments // [])[].body)' 2>/dev/null ; } \
          | grep -oiE '(follow-?up|phase [2-9]|part [2-9]|split out|tracked (in|as|by))[^#]{0,60}#[0-9]+' \
          | grep -vE "#$N\$" | head -1)"
  [ -n "$hit" ]
}

phase_guard_ok() {  # $1=pr -> 0 when merging is safe (guard off / nothing closed / scope complete)
  [ "$PHASE_GUARD" = "1" ] || return 0
  local P="$1" N leftover
  N="$(issue_for_pr "$P")"
  [ -n "$N" ] || return 0                      # closes nothing -> nothing can be dropped
  leftover="$(phase_leftover "$N")"
  [ -n "$leftover" ] || return 0
  phase_followup_linked "$P" "$N" && return 0   # the remainder is already tracked

  # Unchecked boxes alone: warn once and let it merge — see the note above.
  case "$leftover" in
    boxes:*)
      if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: phase guard would warn on PR #$P (issue #$N, $leftover)."; return 0; fi
      if ! gh pr view "$P" --repo "$SLUG" --json comments --jq '((.comments // [])[].body)' 2>/dev/null | grep -qF "$PHASE_GUARD_MARKER"; then
        gh pr comment "$P" --repo "$SLUG" --body "$PHASE_GUARD_MARKER — heads up: issue #$N still has **$leftover** acceptance box(es). Merging anyway (unticked boxes are common here), but if any of that scope did NOT ship, file a follow-up issue with \`agent:ready\` and link it, or drop the \`Closes #$N\` from this PR." >/dev/null 2>&1
      fi
      return 0 ;;
  esac

  # Explicit later phase with nothing tracking it: this is the #548 failure mode — hold it.
  log "PHASE GUARD: PR #$P closes issue #$N which declares a later phase ($leftover) and no follow-up is linked — parking instead of merging."
  if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: would park PR #$P + escalate issue #$N (phase guard)."; return 1; fi
  if ! gh pr view "$P" --repo "$SLUG" --json comments --jq '((.comments // [])[].body)' 2>/dev/null | grep -qF "$PHASE_GUARD_MARKER"; then
    gh pr comment "$P" --repo "$SLUG" --body "$PHASE_GUARD_MARKER — **held before merge.** This PR closes issue #$N, whose body declares work beyond this PR ($leftover), and no follow-up issue is linked anywhere on this PR or that issue. Merging now would close #$N and drop that scope silently — exactly how #548 was lost.

To clear this, do ONE of:
1. File the follow-up issue (topical labels + \`agent:ready\` + a \`priority:\`) and link it here as \`Follow-up: #<n>\`, then re-label this PR \`agent:working\`; or
2. Remove \`Closes #$N\` from the PR body, say what remains, and re-label this PR \`agent:working\` — the issue stays open.

Assigning @$ASSIGNEE." >/dev/null 2>&1
  fi
  gh pr edit "$P" --repo "$SLUG" --add-label needs-human --add-label agent:blocked --remove-label agent:working >/dev/null 2>&1
  gh issue edit "$N" --repo "$SLUG" --add-label needs-human --add-assignee "$ASSIGNEE" >/dev/null 2>&1
  gh pr edit "$P" --repo "$SLUG" --add-assignee "$ASSIGNEE" >/dev/null 2>&1
  return 1
}

add_worktree() {  # $1=branch  $2=base(ref)  -> path on stdout
  local branch="$1" base="$2" wt="$WORKROOT/$1"
  git -C "$REPO" worktree remove --force "$wt" >/dev/null 2>&1 || true
  rm -rf "$wt"
  git -C "$REPO" worktree prune >/dev/null 2>&1
  if git -C "$REPO" show-ref --verify --quiet "refs/remotes/origin/$branch"; then
    git -C "$REPO" worktree add "$wt" "origin/$branch" >/dev/null 2>&1
    git -C "$wt" checkout -B "$branch" "origin/$branch" >/dev/null 2>&1
  else
    git -C "$REPO" worktree add -b "$branch" "$wt" "$base" >/dev/null 2>&1
  fi
  echo "$wt"
}

# Per-issue model tier. Explicit labels always win; without one, only a NARROW class of work is
# auto-downgraded (low-priority docs/cleanup) — everything else inherits the CLI default, so the
# quality of normal feature/bug work is untouched. Owner opt-in per issue:
#   gh issue edit N --add-label agent:model:sonnet     (or :haiku / :opus)
model_for_issue() {  # $1=issue -> echoes model name or nothing (= CLI default)
  [ -n "${1:-}" ] || return 0
  local labels
  labels="$(gh issue view "$1" --repo "$SLUG" --json labels --jq '[.labels[].name]|join(" ")' 2>/dev/null)"
  # Side effect: publish the label set + a priority slug into the env so dispatch_lane's Ollama
  # tier selection (agent:tier:*) and PostHog issue_priority get them with no extra gh call.
  ISSUE_LABELS="$labels"; export ISSUE_LABELS
  ISSUE_PRIORITY="$(printf '%s' "$labels" | grep -oE 'priority:[a-z]+' | head -1)"; export ISSUE_PRIORITY
  case " $labels " in
    *" agent:model:haiku "*)  echo haiku;  return ;;
    *" agent:model:sonnet "*) echo sonnet; return ;;
    *" agent:model:opus "*)   echo opus;   return ;;
  esac
  if echo " $labels " | grep -q " priority:low " \
     && echo " $labels " | grep -qE " (documentation|cleanup) "; then
    echo sonnet
  fi
}

run_claude() {  # $1=worktree  $2=prompt  $3=model (optional; empty = CLI default)
  # Delegates to lib/run_lane.sh, which runs the capacity-aware dispatch (Claude lane = owner's
  # Max subscription; Ollama lane = same `claude -p` CLI pointed at LiteLLM with a lem-agent-tierN
  # alias), emits PostHog lifecycle/AI telemetry, records the lane outcome for the capacity gauge,
  # and applies the ai:* lane/model labels to the issue/PR. All MODE call sites are unchanged.
  run_lane "$@"
}

# --- state machine ---
git -C "$REPO" fetch origin --prune >/dev/null 2>&1

# ---- PRIORITY LANE: Dependabot CI failures (labeled agent:depfix by the router workflow) ----
# Handled before roadmap work so dependency PRs get unblocked fast. One Claude call per tick.
DEPFIX="$(gh pr list --repo "$SLUG" --state open --label "agent:depfix" \
  --json number,headRefName,labels \
  | jq -r 'map(select((.labels|map(.name))|index("needs-human")|not))|.[0]//empty|@json')"
if [ -n "$DEPFIX" ]; then
  DPR="$(echo "$DEPFIX" | jq -r .number)"
  DBR="$(echo "$DEPFIX" | jq -r .headRefName)"
  CLAUDE_TRIES="$(git -C "$REPO" log "origin/$DBR" --grep='Co-Authored-By: Claude' --format=%h 2>/dev/null | wc -l | tr -d ' ')"
  if [ "${CLAUDE_TRIES:-0}" -ge 3 ]; then
    log "Dependabot PR #$DPR still failing after $CLAUDE_TRIES Claude attempts — escalating."
    if [ "$DRY_RUN" != "1" ]; then
      gh pr edit "$DPR" --repo "$SLUG" --add-label needs-human --remove-label agent:depfix >/dev/null 2>&1
      gh issue comment "$DPR" --repo "$SLUG" --body "🚧 Claude couldn't fix CI after $CLAUDE_TRIES attempts on this Dependabot PR. Assigning @$ASSIGNEE." >/dev/null 2>&1
      gh pr edit "$DPR" --repo "$SLUG" --add-assignee "$ASSIGNEE" >/dev/null 2>&1
    fi
    exit 0
  fi
  log "Dependabot PR #$DPR failing — invoking depfix (priority lane, try $((CLAUDE_TRIES+1)))."
  if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: would run MODE=depfix for #$DPR ($DBR)."; exit 0; fi
  if ! claim_branch "$DBR"; then
    log "Dependabot PR #$DPR already claimed by another slot — moving on."
  else
    WT="$(add_worktree "$DBR" origin/main)"
    export MODE=depfix PR="$DPR" BRANCH="$DBR" WORKTREE="$WT"
    run_claude "$WT" "Read $RUNBOOK and follow MODE=depfix. PR=$DPR BRANCH=$DBR."
    exit 0
  fi
fi

# ---- ANSWER-DETECT LANE: owner answered a Decision Comment (on the PR **or** on the issue) ----
# The owner unblocks held work by replying in the thread — no label juggling — and it works on either
# thread, because a Decision Comment can live on either (MODE=start posts one on both when it parks
# risky work, and an escalation before any PR exists has only the issue). Accepted shapes:
#   (a) bare tokens / "ok"          -> "1A 2B", "ok"
#   (b) tokens + context or extras  -> "1A 2C (also research hosted grids) 3A"
#   (c) explicit directive          -> first line starts with "@claude", "decision:" or "go:", for an
#                                      answer that does NOT match any option the agent offered
# Detection + guards live in newest_owner_answer() / answer_verdict() so the two lanes can't drift.
# A `hold`/`question` verdict leaves the work parked for the human — ambiguity never starts a build.
route_owner_answer() {  # route_owner_answer pr|issue <number>
  local KIND="$1" NUM="$2" ANSC LB VERDICT ANS KLBL
  [ "$KIND" = "pr" ] && KLBL="PR" || KLBL="Issue"
  ANSC="$(newest_owner_answer "$KIND" "$NUM")"
  [ -n "$ANSC" ] || return 0
  LB="$(echo "$ANSC" | base64 -d 2>/dev/null)"
  [ -n "$LB" ] || return 0
  VERDICT="$(answer_verdict "$LB")"
  ANS="$(echo "$LB" | tr -d '\n' | cut -c1-60)"
  case "$VERDICT" in
    hold)     log "$KLBL #$NUM — owner reply leads with an answer but asks to hold — leaving parked." ; return 0 ;;
    question) log "$KLBL #$NUM — owner reply is free-form and ends in a question — leaving parked." ; return 0 ;;
    "")       return 0 ;;
  esac
  # Which thread the answer landed on doesn't matter — route the WORK. If a PR exists it goes to the
  # revise lane; if the issue was parked before any PR existed, it goes back on the ready queue and
  # MODE=start reads the answer off the issue.
  local TPR TISS
  if [ "$KIND" = "pr" ]; then
    TPR="$NUM"; TISS="$(issue_for_pr "$NUM")"
  else
    TISS="$NUM"; TPR="$(pr_for_issue "$NUM")"
  fi
  if [ -n "$TPR" ]; then
    log "$KLBL #$NUM — owner answered Decision Comment ($VERDICT: '$ANS') — routing PR #$TPR to revise."
    if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: would label PR #$TPR agent:revise${TISS:+ and issue #$TISS agent:working}."; else
      gh pr edit "$TPR" --repo "$SLUG" --add-label agent:revise \
        --remove-label needs-human --remove-label agent:blocked >/dev/null 2>&1
      # Mirror onto the issue so it stops reading as parked while the PR is being revised.
      [ -n "$TISS" ] && gh issue edit "$TISS" --repo "$SLUG" --add-label agent:working \
        --remove-label needs-human --remove-label agent:blocked >/dev/null 2>&1
    fi
  else
    # An issue still parked AFTER its PR merged must not go back on the queue — that redoes shipped
    # work. That state means the issue just needs closing, which is a human call.
    local DONEPR
    DONEPR="$(gh issue view "$TISS" --repo "$SLUG" --json closedByPullRequestsReferences 2>/dev/null \
              | jq -r '[(.closedByPullRequestsReferences // [])[] | .number] | (first // empty)')"
    if [ -n "$DONEPR" ] \
       && [ "$(gh pr view "$DONEPR" --repo "$SLUG" --json state --jq .state 2>/dev/null)" = "MERGED" ]; then
      log "$KLBL #$NUM — answered, but its PR #$DONEPR is already MERGED — leaving parked (needs closing, not redoing)."
      return 0
    fi
    log "$KLBL #$NUM — owner answered Decision Comment ($VERDICT: '$ANS'), no PR yet — returning issue #$TISS to the ready queue."
    if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: would label issue #$TISS agent:ready."; else
      gh issue edit "$TISS" --repo "$SLUG" --add-label agent:ready \
        --remove-label needs-human --remove-label agent:blocked >/dev/null 2>&1
    fi
  fi
}

for HPR in $(gh pr list --repo "$SLUG" --state open --label "needs-human" \
               --json number --jq '.[].number' 2>/dev/null); do
  route_owner_answer pr "$HPR"
done

# Same lane for issues: the owner (or a future contributor with write access) may answer on the issue
# thread instead — either because that's where the Decision Comment is, or just out of habit.
for HISSUE in $(gh issue list --repo "$SLUG" --state open --limit 100 --label "needs-human" \
                  --json number --jq '.[].number' 2>/dev/null); do
  route_owner_answer issue "$HISSUE"
done

# ---- MERGE-READY FAST PATH: merge any agent:working PR that ALREADY fully passes the gate,
# BEFORE spending a tick on slow revise/rebase/fix/review work. Without this, a backlog of
# agent:revise PRs (or a DIRTY PR that open_agent_pr prefers) starves a green, review-clean PR
# from ever reaching the merge step for hours. Uses the EXACT same gate as step 5 below — no
# weakening: required checks green, zero unresolved Copilot threads, Copilot reviewed the current
# head OR the head is past the grace window. First fully-ready PR is merged, then the tick exits.
for MPR in $(gh pr list --repo "$SLUG" --state open --label "agent:working" \
               --json number --jq 'sort_by(.number)|.[].number' 2>/dev/null); do
  MBR="$(gh pr view "$MPR" --repo "$SLUG" --json headRefName --jq .headRefName 2>/dev/null)"
  [ -z "$MBR" ] && continue
  [ "$(gh pr view "$MPR" --repo "$SLUG" --json mergeStateStatus --jq .mergeStateStatus 2>/dev/null)" = "DIRTY" ] && continue
  MROLL="$(gh pr view "$MPR" --repo "$SLUG" --json statusCheckRollup 2>/dev/null \
    | jq -r '[.statusCheckRollup[]? | {n:(.name//.context//""), s:(.conclusion//.state//"PENDING")}
             | select(.n=="Unit Tests (Python 3.12)" or .n=="Integration Tests" or .n=="GitGuardian Scan" or .n=="UI Build" or .n=="Migration Versions")]')"
  [ "$(echo "$MROLL" | jq '[.[]|select(.s=="FAILURE" or .s=="ERROR" or .s=="TIMED_OUT" or .s=="CANCELLED")]|length')" -gt 0 ] && continue
  [ "$(echo "$MROLL" | jq '[.[]|select(.s=="PENDING" or .s=="QUEUED" or .s=="IN_PROGRESS" or .s=="EXPECTED")]|length')" -gt 0 ] && continue
  [ "$(copilot_unresolved_threads "$MPR")" -gt 0 ] && continue
  MHD="$(git -C "$REPO" log -1 --format=%cI "origin/$MBR" 2>/dev/null)"
  # Fast path only merges PRs whose review evidence (Copilot review OR Claude adversarial marker)
  # already exists and is fresh — arranging a missing review is the main lane's job.
  MBEST="$(best_review_at "$MPR")"
  [ -z "$MBEST" ] && continue
  if [ "$(epoch "$MBEST")" -lt "$(epoch "$MHD")" ] \
     && [ "$(( $(date +%s) - $(epoch "$MHD") ))" -lt "${REVIEW_GRACE_SECONDS:-1200}" ]; then continue; fi
  # Last gate before any merge: closing an issue must not drop a declared later phase.
  phase_guard_ok "$MPR" || exit 0
  log "MERGE-READY FAST PATH: PR #$MPR fully passes the gate — merging ahead of revise/fix work."
  if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: would fast-path merge #$MPR."; exit 0; fi
  gh pr merge --auto "$(gh pr view "$MPR" --repo "$SLUG" --json url --jq .url)" >/dev/null 2>&1 \
    && gh pr comment "$MPR" --repo "$SLUG" --body "✅ CI green, review satisfied & all threads resolved — merging (fast path)." >/dev/null 2>&1
  exit 0
done

# ---- REVISE LANE: the owner reviewed a PR and requested changes (label agent:revise) ----
# Claude implements the OWNER's feedback (not Copilot's), then hands the PR forward to merge.
# Iterates candidates so a claim held by another slot doesn't starve the rest of the lane.
for RJSON in $(gh pr list --repo "$SLUG" --state open --label "agent:revise" \
  --json number,headRefName --jq 'sort_by(.number)|.[]|@base64' 2>/dev/null); do
  RPR="$(echo "$RJSON" | base64 -d | jq -r .number)"
  RBR="$(echo "$RJSON" | base64 -d | jq -r .headRefName)"
  if ! claim_branch "$RBR"; then
    log "PR #$RPR (revise) claimed by another slot — trying next."
    continue
  fi
  log "PR #$RPR — owner requested changes (agent:revise) — implementing their feedback."
  if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: would run MODE=revise for #$RPR ($RBR)."; exit 0; fi
  WT="$(add_worktree "$RBR" origin/main)"
  export MODE=revise PR="$RPR" BRANCH="$RBR" WORKTREE="$WT"
  run_claude "$WT" "Read $RUNBOOK and follow MODE=revise. PR=$RPR BRANCH=$RBR OWNER=$ASSIGNEE."
  exit 0
done

# ---- IN-FLIGHT PRs: iterate every agent:working PR (DIRTY first, then by number). A PR that's
# merely WAITING (CI running, review grace) is skipped so a slot can do useful work on the next
# one — or start a new issue — instead of idling behind it. Actionable paths claim the branch so
# concurrent slots never collide.
WORKING_PRS=0
for PR_JSON in $(gh pr list --repo "$SLUG" --state open --label "agent:working" \
    --json number,headRefName,mergeStateStatus \
    --jq 'sort_by((if .mergeStateStatus=="DIRTY" then 0 else 1 end), .number)|.[]|@base64' 2>/dev/null); do
  WORKING_PRS=$((WORKING_PRS + 1))
  PR="$(echo "$PR_JSON" | base64 -d | jq -r .number)"
  BRANCH="$(echo "$PR_JSON" | base64 -d | jq -r .headRefName)"
  MSTATE="$(echo "$PR_JSON" | base64 -d | jq -r .mergeStateStatus)"
  ISSUE="$(issue_for_pr "$PR")"
  HEAD_DATE="$(git -C "$REPO" log -1 --format=%cI "origin/$BRANCH" 2>/dev/null)"
  log "In-flight PR #$PR (branch $BRANCH, issue #${ISSUE:-?})."

  # 0) Stale/conflicting with main (went dirty while other PRs merged) -> rebase before anything else.
  if [ "$MSTATE" = "DIRTY" ]; then
    if ! claim_branch "$BRANCH"; then log "PR #$PR claimed by another slot — trying next."; continue; fi
    if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: PR #$PR DIRTY — would try a scripted rebase, else MODE=rebase."; exit 0; fi
    WT="$(add_worktree "$BRANCH" origin/main)"
    # Try a SCRIPTED clean rebase FIRST — no Claude spend when the branch replays onto main without
    # conflicts (GitHub flags DIRTY conservatively; a 3-way rebase often applies cleanly). Only real
    # conflicts escalate to Claude. CI (+ the Migration Versions check) backstops a clean-but-wrong rebase.
    if git -C "$WT" rebase origin/main >/dev/null 2>&1; then
      if git -C "$WT" push --force-with-lease origin "$BRANCH" >/dev/null 2>&1; then
        log "PR #$PR — scripted clean rebase onto main (no Claude); CI/Copilot re-triggered."
        exit 0
      fi
      log "PR #$PR — scripted rebase clean but push rejected — falling back to Claude MODE=rebase."
    else
      git -C "$WT" rebase --abort >/dev/null 2>&1
      log "PR #$PR — real conflicts — invoking Claude MODE=rebase."
    fi
    export MODE=rebase PR ISSUE WORKTREE="$WT" BRANCH
    run_claude "$WT" "Read $RUNBOOK and follow MODE=rebase. PR=$PR ISSUE=$ISSUE BRANCH=$BRANCH."
    exit 0
  fi

  # Only the branch-protection REQUIRED checks gate merge — ignore non-required noise (CodeQL, E2E, lint).
  ROLLUP="$(gh pr view "$PR" --repo "$SLUG" --json statusCheckRollup \
    | jq -r '[.statusCheckRollup[]? | {n:(.name//.context//""), s:(.conclusion//.state//"PENDING")}
             | select(.n=="Unit Tests (Python 3.12)" or .n=="Integration Tests" or .n=="GitGuardian Scan" or .n=="UI Build" or .n=="Migration Versions")]')"
  FAILED="$(echo "$ROLLUP" | jq '[.[]|select(.s=="FAILURE" or .s=="ERROR" or .s=="TIMED_OUT" or .s=="CANCELLED")]|length')"
  PENDING="$(echo "$ROLLUP" | jq '[.[]|select(.s=="PENDING" or .s=="QUEUED" or .s=="IN_PROGRESS" or .s=="EXPECTED")]|length')"
  ATTEMPTS="$(git -C "$REPO" rev-list --count "origin/main..origin/$BRANCH" 2>/dev/null || echo 1)"
  UNRESOLVED="$(copilot_unresolved_threads "$PR")"
  CP_AT="$(copilot_last_review_at "$PR")"

  # 1) CI failing -> fix (or escalate after too many tries)
  if [ "${FAILED:-0}" -gt 0 ]; then
    if ! claim_branch "$BRANCH"; then log "PR #$PR claimed by another slot — trying next."; continue; fi
    if [ "${ATTEMPTS:-1}" -ge "$MAX_FIX_ATTEMPTS" ]; then
      log "PR #$PR failing after $ATTEMPTS attempts — escalating to human."
      if [ "$DRY_RUN" != "1" ]; then
        gh pr edit "$PR" --repo "$SLUG" --add-label needs-human --add-label agent:blocked --remove-label agent:working >/dev/null 2>&1
        gh pr ready --undo "$PR" --repo "$SLUG" >/dev/null 2>&1
        [ -n "$ISSUE" ] && gh issue edit "$ISSUE" --repo "$SLUG" --add-label needs-human --add-label agent:blocked --add-assignee "$ASSIGNEE" --remove-label agent:working >/dev/null 2>&1
        if ! auto_fix_gave_up_p "$PR"; then
          gh pr comment "$PR" --repo "$SLUG" --body "🚧 Auto-fix gave up after $ATTEMPTS attempts. Assigning @$ASSIGNEE — CI is still red." >/dev/null 2>&1
        fi
      fi
      exit 0
    fi
    log "PR #$PR CI failing (attempt $ATTEMPTS) — invoking fix."
    WT="$(add_worktree "$BRANCH" origin/main)"
    export MODE=fix PR ISSUE WORKTREE="$WT" BRANCH ATTEMPTS
    run_claude "$WT" "Read $RUNBOOK and follow MODE=fix. PR=$PR ISSUE=$ISSUE BRANCH=$BRANCH ATTEMPTS=$ATTEMPTS." "$(model_for_issue "$ISSUE")"
    exit 0
  fi

  # 2) Copilot has unresolved review threads -> address + resolve them BEFORE any merge
  if [ "${UNRESOLVED:-0}" -gt 0 ]; then
    if ! claim_branch "$BRANCH"; then log "PR #$PR claimed by another slot — trying next."; continue; fi
    log "PR #$PR — $UNRESOLVED unresolved Copilot thread(s) — invoking review-address."
    WT="$(add_worktree "$BRANCH" origin/main)"
    export MODE=review PR ISSUE WORKTREE="$WT" BRANCH
    run_claude "$WT" "Read $RUNBOOK and follow MODE=review. PR=$PR ISSUE=$ISSUE BRANCH=$BRANCH." "$(model_for_issue "$ISSUE")"
    exit 0
  fi

  # 3) CI still running -> not actionable; let this slot look at the next PR / start new work.
  if [ "${PENDING:-0}" -gt 0 ]; then
    log "PR #$PR — CI still running ($PENDING pending). Skipping to next work."
    continue
  fi

  # 4) CI green + no unresolved Copilot threads. The gate needs ONE fresh review — Copilot's
  #    (selective: risk:*/review:copilot PRs, requested once after green) or Claude's adversarial
  #    marker (the default reviewer, run on the owner's flat-rate Max login). A review older than
  #    the head is tolerated once the head has been stable past the grace window (nobody
  #    re-reviews every push; unresolved threads on new code are caught by step 2).
  REVIEW_GRACE_SECONDS="${REVIEW_GRACE_SECONDS:-1200}"   # 20 min
  BEST_AT="$(best_review_at "$PR")"
  REVIEW_OK=0
  if [ -n "$BEST_AT" ]; then
    if [ "$(epoch "$BEST_AT")" -ge "$(epoch "$HEAD_DATE")" ] \
       || [ "$(( $(date +%s) - $(epoch "$HEAD_DATE") ))" -ge "$REVIEW_GRACE_SECONDS" ]; then
      REVIEW_OK=1
    fi
  fi
  if [ "$REVIEW_OK" != "1" ]; then
    if copilot_wanted "$PR"; then
      # This PR merits the budgeted Copilot pass: request once, wait up to the timeout, then fall
      # back per REVIEW_FALLBACK (claude = adversarial review; merge = ⚠️ merge; hold = wait).
      if [ -z "$CP_AT" ]; then try_request_copilot_review "$PR"; fi
      if ! review_wait_expired "$HEAD_DATE"; then
        log "PR #$PR — green; waiting for Copilot review (risk/review:copilot PR). Skipping to next work."
        continue
      fi
      case "$REVIEW_FALLBACK" in
        hold)
          log "PR #$PR — Copilot review overdue; REVIEW_FALLBACK=hold — waiting."
          continue ;;
        merge)
          phase_guard_ok "$PR" || exit 0
          log "PR #$PR — Copilot review overdue; REVIEW_FALLBACK=merge — merging with warning."
          if [ "$DRY_RUN" != "1" ]; then
            gh pr merge --auto "$(gh pr view "$PR" --repo "$SLUG" --json url --jq .url)" >/dev/null 2>&1 \
              && gh pr comment "$PR" --repo "$SLUG" --body "⚠️ Merging with CI green but WITHOUT a review — Copilot never delivered and REVIEW_FALLBACK=merge." >/dev/null 2>&1
          fi
          exit 0 ;;
      esac
      # REVIEW_FALLBACK=claude falls through to the adversarial review below.
    fi
    # Default reviewer: Claude adversarial review (fresh context; finds AND fixes; posts the
    # marker comment the gate accepts). One run per stale/missing review.
    if claude_marker_fresh_p "$PR" "$HEAD_DATE"; then
      log "PR #$PR — green, fresh Claude adversarial marker already present."
    else
      if ! claim_branch "$BRANCH"; then log "PR #$PR claimed by another slot — trying next."; continue; fi
      log "PR #$PR — green, no fresh review — invoking Claude adversarial review."
      if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: would run MODE=selfreview for #$PR."; exit 0; fi
      WT="$(add_worktree "$BRANCH" origin/main)"
      export MODE=selfreview PR ISSUE WORKTREE="$WT" BRANCH
      run_claude "$WT" "Read $RUNBOOK and follow MODE=selfreview. PR=$PR ISSUE=$ISSUE BRANCH=$BRANCH MARKER='$CLAUDE_REVIEW_MARKER'." "$(model_for_issue "$ISSUE")"
      exit 0
    fi
  fi

  # 5) Green + fresh review (Copilot or Claude marker) + all threads resolved -> merge.
  # Same last gate as the fast path: never close an issue that still declares a later phase.
  phase_guard_ok "$PR" || exit 0
  log "PR #$PR — merge gate satisfied. Merging."
  if [ "$DRY_RUN" != "1" ]; then
    gh pr merge --auto "$(gh pr view "$PR" --repo "$SLUG" --json url --jq .url)" >/dev/null 2>&1 \
      && gh pr comment "$PR" --repo "$SLUG" --body "✅ CI green, review satisfied & all threads resolved — merging." >/dev/null 2>&1
  fi
  exit 0
done

# ---- START NEXT ISSUE — gated on WIP so concurrency never outruns merge throughput: only start
# new work while the number of in-flight agent PRs is below the slot cap.
if [ "$WORKING_PRS" -ge "$CAP" ]; then
  log "WIP limit: $WORKING_PRS in-flight PR(s) >= cap $CAP — not starting new work."
  exit 0
fi
# Degraded mode: don't START expensive new implementation when both lanes are constrained —
# prioritize triage/merge/answer work (already done above). Hold new starts until a lane recovers.
if [ "${DEGRADED:-0}" = "1" ]; then
  log "DEGRADED: holding new issue start (both lanes constrained). Triage/merge/answer lanes ran."
  exit 0
fi
ISSUE="$(select_next_issue)"
if [ -z "$ISSUE" ]; then
  log "No agent:ready issues remaining. Pipeline idle."
  exit 0
fi
RISK="$(gh issue view "$ISSUE" --repo "$SLUG" --json labels \
        | jq -r '[.labels[].name|select(startswith("risk:"))|sub("risk:";"")]|join(" ")')"
[ -z "$RISK" ] && RISK="none"
BRANCH="feature/claude-issue-$ISSUE"
if ! claim_branch "$BRANCH"; then
  log "Issue #$ISSUE branch already claimed by another slot — skipping."
  exit 0
fi
MODEL="$(model_for_issue "$ISSUE")"
log "Starting issue #$ISSUE (risk=$RISK${MODEL:+, model=$MODEL}) on $BRANCH."
posthog_capture "issue_queued" "agent-pipeline" "{\"issue_number\":$ISSUE,\"issue_url\":\"https://github.com/$SLUG/issues/$ISSUE\",\"worker_id\":\"${WORKER_ID:-}\",\"issue_priority\":\"${ISSUE_PRIORITY:-}\",\"issue_type\":\"start\"}" 2>/dev/null || true
if [ "$DRY_RUN" = "1" ]; then
  log "DRY_RUN: would create worktree $BRANCH and run MODE=start for #$ISSUE."
  exit 0
fi
gh issue edit "$ISSUE" --repo "$SLUG" --add-label agent:working --remove-label agent:ready >/dev/null 2>&1
WT="$(add_worktree "$BRANCH" origin/main)"
export MODE=start ISSUE WORKTREE="$WT" BRANCH RISK
run_claude "$WT" "Read $RUNBOOK and follow MODE=start. ISSUE=$ISSUE BRANCH=$BRANCH RISK=$RISK WORKTREE=$WT." "$MODEL"
exit 0
