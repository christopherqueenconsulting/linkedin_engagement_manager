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
MAX_PHASEFIX_ATTEMPTS=2
# Budgets for the three lanes that previously had NONE — each retried a failing run every tick
# forever, so one wedged PR could burn a 45-minute Claude run per tick indefinitely. Counted in
# DISPATCHED RUNS via lib/ledger.sh (charged before the run starts, so a timeout consumes budget
# too); the owner's Decision-Comment answer resets them (route_owner_answer).
MAX_REVISE_ATTEMPTS="${MAX_REVISE_ATTEMPTS:-2}"
MAX_REVIEW_ATTEMPTS="${MAX_REVIEW_ATTEMPTS:-3}"
MAX_SELFREVIEW_ATTEMPTS="${MAX_SELFREVIEW_ATTEMPTS:-2}"
CLAUDE_TIMEOUT="45m"
DRY_RUN="${DRY_RUN:-0}"

# The checks branch protection ACTUALLY requires on main. Verify with:
#   gh api repos/:owner/:repo/branches/main/protection --jq '.required_status_checks.contexts'
#
# Defined once because it was previously spelled out twice, and both copies had drifted from the
# real list: they omitted "CodeQL PR Quality Gate", so the pipeline would call a PR green and
# request a merge while a required check was still pending or failing — and then sit in the queue.
# Note this is the PR *Quality Gate*, not "CodeQL Security Analysis", which runs but is NOT required.
REQUIRED_CHECKS_JQ='select(.n=="Unit Tests (Python 3.12)" or .n=="Integration Tests" or .n=="GitGuardian Scan" or .n=="UI Build" or .n=="Migration Versions" or .n=="CodeQL PR Quality Gate")'

# Owner-tunable knobs (edit $BASE/config.env; missing file = these defaults).
#   MAX_AGENTS         hard ceiling on concurrent Claude runs (slots), whatever the backlog
#   SCALE_PER_ISSUES   +1 slot per this many agent:ready issues (1 + N/SCALE, capped)
#   BUSY_HOURS         e.g. "10-17": during these hours the cap drops to BUSY_MAX_AGENTS so
#                      pipeline agents don't compete with the owner's interactive Claude usage
#   BUSY_TZ            IANA zone the BUSY_HOURS are expressed in, e.g. "America/New_York".
#                      Empty = UTC. Use a ZONE, not a fixed UTC offset: a hard-coded UTC range
#                      silently slides by an hour at each DST change, so a window set in summer
#                      covers the wrong hours all winter.
#   BUSY_DAYS          e.g. "1-5" for Mon-Fri (date +%u: 1=Mon .. 7=Sun). Empty = every day.
#   BUSY_MAX_AGENTS    slots allowed during the busy window (default 1 = the old behaviour)
#   BUSY_HOURS_UTC     DEPRECATED alias for BUSY_HOURS with BUSY_TZ=UTC. Honoured so an
#                      un-migrated config.env keeps its guard instead of silently losing it.
#   USAGE_PAUSE_MINUTES how long to self-pause when a run hits a usage/rate limit
#   PHASE_GUARD        1 (default) = hold a merge that would close an issue with a declared,
#                      untracked later phase; 0 = off (see "Phase guard" below)
#   MERGE_STALE_TICKS  consecutive ticks a requested merge may sit with no live merge-queue entry
#                      before the lane clears the dangling state and re-enqueues (default 3)
#   MERGE_QUEUE_STUCK_TICKS  merge requests one head SHA may burn while the queue keeps taking and
#                      dropping it, before the tick reports itself failed (default 12, ~1h)
[ -f "$BASE/config.env" ] && . "$BASE/config.env"
MAX_AGENTS="${MAX_AGENTS:-3}"
SCALE_PER_ISSUES="${SCALE_PER_ISSUES:-10}"
BUSY_HOURS_UTC="${BUSY_HOURS_UTC:-}"
BUSY_HOURS="${BUSY_HOURS:-$BUSY_HOURS_UTC}"   # new name; falls back to the deprecated one
BUSY_TZ="${BUSY_TZ:-UTC}"                     # so a bare BUSY_HOURS_UTC keeps meaning UTC
BUSY_DAYS="${BUSY_DAYS:-}"
BUSY_MAX_AGENTS="${BUSY_MAX_AGENTS:-1}"
USAGE_PAUSE_MINUTES="${USAGE_PAUSE_MINUTES:-60}"

export PATH="/home/lem/.local/bin:/usr/local/bin:/usr/bin:/bin"
# cron supplies no locale, so anything touching non-ASCII text (agent prompts, PR bodies, comment
# markers) runs under the C locale and mangles it. Set one explicitly.
export LANG="${LANG:-C.UTF-8}" LC_ALL="${LC_ALL:-C.UTF-8}"
export HOME="/home/lem"
export GH_PROMPT_DISABLED=1

# The pipeline's OWN credential, when one is configured (AGENT_GH_TOKEN in config.env).
#
# The stored `gh auth login` token is the owner's, and it carries the `workflow` scope — so the
# agent can rewrite the very workflows that gate merges and deploys. CODEOWNERS makes that
# reviewable; only the token makes it impossible, and impossible is the property worth having when
# the thing holding the credential is reading issue text written by strangers.
#
# Setting GH_TOKEN covers BOTH halves: `gh` prefers it over the stored auth, and git is configured
# with `gh auth git-credential` as its credential helper, so pushes use it too.
#
# Wanted shape — a fine-grained PAT, THIS repo only:
#   Contents: R/W · Pull requests: R/W · Issues: R/W · Metadata: R
#   Workflows: NO ACCESS  ← the point
#   no Administration, no Packages, no Secrets, no Environments
[ -n "${AGENT_GH_TOKEN:-}" ] && export GH_TOKEN="$AGENT_GH_TOKEN"

mkdir -p "$WORKROOT" "$LOGDIR" "$BASE/locks" "$BASE/state"
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
for _l in posthog labels capacity dispatch run_lane gh_app_token ledger; do . "$BASE/lib/$_l.sh" 2>/dev/null || true; done

# IDENTITY: prefer the GitHub App over the owner's PAT (USE_GH_APP=1 in config.env). The PAT acts
# as the OWNER, which makes an owner-approval gate on outside contributions impossible to build
# (GitHub forbids self-approval, so every agent PR would be permanently red) AND pointless (a
# prompt-injected run could approve an attacker's PR as the owner). The app can author and merge
# but can never approve. Falls back to the PAT on any failure — a missing key or a GitHub blip
# must degrade the pipeline's IDENTITY, never its ability to run.
if command -v gh_app_export_token >/dev/null 2>&1 && gh_app_export_token; then
  # GH_TOKEN is now the app's installation token (~1h life, auto-refreshed).
  [ -n "${GH_APP_BOT_LOGIN:-}" ] || log "GH APP: identity active but the bot login could not be resolved (GET /app) — the trust boundary will refuse this tick's own labels. Pin GH_APP_BOT_LOGIN in secrets.env."
elif [ "${USE_GH_APP:-0}" = "1" ]; then
  log "GH APP: USE_GH_APP=1 but no installation token could be minted — falling back to AGENT_GH_TOKEN. Check $BASE/secrets/github-app.pem and GH_APP_ID."
fi

# A box whose lib/ predates ledger.sh (the window between a tick.sh sync and its lib landing, or
# a DRY_RUN from a checkout) must degrade to the OLD behavior — unbudgeted but working — never to
# "command not found" inside a lane. Count 0 = budgets never trip; charge is a no-op.
if ! command -v ledger_count >/dev/null 2>&1; then
  # LOUD, not silent: this fallback removes every run budget, which is the failure mode the ledger
  # exists to prevent. A box running unbudgeted for days because lib/ledger.sh never synced must be
  # readable in the tick log, not inferred from a runaway spend.
  log "LEDGER: lib/ledger.sh not loaded — running UNBUDGETED this tick (no lane budget can trip). Sync $BASE/lib/."
  ledger_count() { echo 0; }
  ledger_charge() { echo 1; }
  ledger_reset() { :; }
fi

# DRY_RUN is read-only on the ledger too. The per-lane `if [ "$DRY_RUN" != "1" ]` guards sit on the
# CHARGE sites only; the resets in the merge loop have none, so a dry tick would clear a live meter
# and let a genuinely exhausted lane dispatch again on the next real tick.
if [ "$DRY_RUN" = "1" ]; then ledger_reset() { :; }; fi

ensure_ai_labels 2>/dev/null || true   # idempotent bootstrap of the ai:* labels (first tick only)
capacity_preflight 2>/dev/null || true  # sets CLAUDE_PCT/OLLAMA_PCT/CLAUDE_AVAIL/OLLAMA_AVAIL/DEGRADED

# --- guards ---
# DRY_RUN is inert (no pushes, no Claude, no merges) so it may validate even while paused.
if [ -f "$PAUSED" ] && [ "$DRY_RUN" != "1" ]; then log "PAUSED file present — skipping."; TICK_OUTCOME="skipped"; TICK_REASON="paused"; exit 0; fi
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
    TICK_OUTCOME="skipped"; TICK_REASON="both_lanes_exhausted"; exit 0 ;;
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
if [ -n "$BUSY_HOURS" ]; then
  # Read the clock in BUSY_TZ, not UTC. `date` resolves the zone's DST rules for us, so a window
  # written as local wall-clock hours stays on those hours year-round instead of sliding by one.
  H=$((10#$(TZ="$BUSY_TZ" date +%H))); DOW=$(TZ="$BUSY_TZ" date +%u)
  B_START="${BUSY_HOURS%-*}"; B_END="${BUSY_HOURS#*-}"
  IN_HOURS=0
  if { [ "$B_START" -le "$B_END" ] && [ "$H" -ge "$B_START" ] && [ "$H" -lt "$B_END" ]; } \
     || { [ "$B_START" -gt "$B_END" ] && { [ "$H" -ge "$B_START" ] || [ "$H" -lt "$B_END" ]; }; }; then
    IN_HOURS=1
  fi
  # Day filter, e.g. "1-5" for Mon-Fri. Empty means every day, which is the pre-2026-08 behaviour.
  IN_DAYS=1
  if [ -n "$BUSY_DAYS" ]; then
    D_START="${BUSY_DAYS%-*}"; D_END="${BUSY_DAYS#*-}"
    { [ "$DOW" -ge "$D_START" ] && [ "$DOW" -le "$D_END" ]; } || IN_DAYS=0
  fi
  if [ "$IN_HOURS" = 1 ] && [ "$IN_DAYS" = 1 ]; then
    [ "$CAP" -gt "$BUSY_MAX_AGENTS" ] && CAP="$BUSY_MAX_AGENTS"
    log "busy window (${BUSY_HOURS} ${BUSY_TZ}${BUSY_DAYS:+, days $BUSY_DAYS}) — cap $CAP"
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
if [ -z "$SLOT" ]; then log "all $CAP agent slot(s) busy — skipping."; TICK_OUTCOME="skipped"; TICK_REASON="all_slots_busy"; exit 0; fi
export WORKER_ID="$SLOT"   # so posthog_capture events tagged with this slot
[ "$CAP" -gt 1 ] && log "slot $SLOT/$CAP (ready issues: $READY_COUNT)"

# Per-branch work claim so concurrent slots never touch the same PR/issue. Re-opening fd 10
# releases any previously held claim (fd close drops the flock).
claim_branch() {  # $1=branch -> 0 claimed, 1 busy
  exec 10>"$BASE/locks/br-$(echo "$1" | tr '/' '_').lock"
  flock -n 10
}

# --- tick outcome → PostHog ---
# One tick_outcome event per tick lifecycle, emitted via an EXIT trap so every code path is
# captured (success, skip, error, kill). Vars are set by the caller before `exit`; the trap
# reads them, builds a JSON dict, and fires posthog_capture. Telemetry is best-effort —
# posthog_capture is fire-and-forget and never breaks a tick.
TICK_T0="$SECONDS"                       # wall-clock seconds since shell start
TICK_OUTCOME="unknown"                   # dispatched | skipped | error | nothing_to_do
TICK_REASON=""                           # free-form: "both_lanes_exhausted", "no_ready", "all_slots_busy", "paused", "all_prs_clean", "mode_start", "mode_fix", "mode_review", "mode_merge", "mode_selfreview", "mode_rebase", "mode_depfix", "mode_docfix", "mode_revise", "mode_phasefix", "escalate"
TICK_MODE=""                             # mode name if a Claude run was dispatched
TICK_ISSUE=""                            # issue number dispatched
TICK_WORKTREE=""                         # worktree this tick created; released by the EXIT trap
TICK_PR=""                               # PR number processed
TICK_BRANCH=""                           # branch
TICK_LANE="${LANE:-}"                    # claude | ollama
TICK_MODEL="${AGENT_MODEL:-${AGENT_TIER:-}}"  # model or tier alias
TICK_ROUTE_REASON="${ROUTE_REASON:-}"    # healthy | fallback | degraded
TICK_AGENT_RC="-1"                       # exit status of the agent run; -1 = no agent ran this tick
TICK_CLAUDE_PCT="${CLAUDE_PCT:-}"
TICK_OLLAMA_PCT="${OLLAMA_PCT:-}"
TICK_READY_COUNT="${READY_COUNT:-0}"
TICK_CAP="${CAP:-1}"
# LANE/MODEL/ROUTE_REASON/AGENT_RC above are placeholders: the routing decision (dispatch_lane) and
# the agent run both happen later, inside run_lane(). run_lane.sh backfills all four before this
# block is read by the EXIT trap.
export TICK_OUTCOME TICK_REASON TICK_MODE TICK_ISSUE TICK_PR TICK_BRANCH TICK_LANE TICK_MODEL TICK_ROUTE_REASON TICK_AGENT_RC TICK_CLAUDE_PCT TICK_OLLAMA_PCT TICK_READY_COUNT TICK_CAP

# Build and emit the tick_outcome event. Reads the var block above; called once by the EXIT
# trap below so every code path (success, skip, error, kill) emits exactly one event.
#
# We also write the event to a local NDJSON mirror at $LOGDIR/tick-outcomes.ndjson in ADDITION
# to PostHog — the local file is the source of truth for FAILURE MODES PostHog can't see (no key,
# network down, project rate-limited). Inspect with:
#   tail -f /home/lem/agent-pipeline/logs/tick-outcomes.ndjson
#   jq -c 'select(.tick_outcome=="dispatched")' .../tick-outcomes.ndjson
TICK_OUTCOMES_LOG="${LOGDIR}/tick-outcomes.ndjson"
emit_tick_outcome() {
  local dur_ms=$(( (SECONDS - TICK_T0) * 1000 ))
  local ts; ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  local payload
  payload="$(__TICK_DUR_MS="$dur_ms" __TICK_TS="$ts" python3 -c '
import json, os
out = {
  "tick_outcome": os.environ.get("TICK_OUTCOME","unknown"),
  "reason":       os.environ.get("TICK_REASON",""),
  "mode":         os.environ.get("TICK_MODE",""),
  "slot":         os.environ.get("WORKER_ID","") or os.environ.get("SLOT",""),
  "ready_count":  int(os.environ.get("TICK_READY_COUNT","0") or 0),
  "cap":          int(os.environ.get("TICK_CAP","1") or 1),
  "lane":         os.environ.get("TICK_LANE",""),
  "model":        os.environ.get("TICK_MODEL",""),
  "route_reason": os.environ.get("TICK_ROUTE_REASON",""),
  "agent_rc":     int(os.environ.get("TICK_AGENT_RC","-1") or -1),
  "claude_pct":   int(os.environ.get("TICK_CLAUDE_PCT","0") or 0),
  "ollama_pct":   int(os.environ.get("TICK_OLLAMA_PCT","0") or 0),
  "issue_number": int(os.environ.get("TICK_ISSUE","0") or 0),
  "pr_number":    int(os.environ.get("TICK_PR","0") or 0),
  "branch":       os.environ.get("TICK_BRANCH",""),
  "duration_ms":  int(os.environ.get("__TICK_DUR_MS","0") or 0),
  "ts":           os.environ.get("__TICK_TS",""),
}
print(json.dumps(out))
' 2>/dev/null)" || payload="{\"tick_outcome\":\"error\"}"
  # Mirror to local NDJSON so the owner can inspect tick outcomes even when PostHog is down.
  [ -n "$TICK_OUTCOMES_LOG" ] && echo "$payload" >> "$TICK_OUTCOMES_LOG" 2>/dev/null || true
  posthog_capture "tick_outcome" "agent-pipeline" "$payload" 2>/dev/null || true
}

# Emit on every exit (success, explicit exit, error). The trap fires LAST; if a code path already
# called emit_tick_outcome inline, the trap call is a no-op for repeat emission (PostHog accepts
# duplicates — the duplicate is harmless and the inline call gives us a single clean event).
# Worktree release rides the SAME trap. Bash allows one handler per signal, so this must extend the
# existing one rather than add a second — a second `trap ... EXIT` would silently replace the
# telemetry. TICK_WORKTREE is set by add_worktree; release_worktree refuses if the branch lock is
# still held or the tree holds uncommitted/unpushed work, so a killed run never loses an agent's
# commits.
trap '__TICK_DUR_MS=$(( (SECONDS - TICK_T0) * 1000 )); emit_tick_outcome; release_worktree "${TICK_WORKTREE:-}"' EXIT

# --- helpers ---
epoch() { date -d "$1" +%s 2>/dev/null || echo 0; }

# --- trust boundary ---------------------------------------------------------------------------
# A LABEL IS NOT AN ACCESS CONTROL. `agent:ready` hands an autonomous agent the owner's credentials
# and a merge to main, but labels have no ACL and this repo is PUBLIC — and three writers could
# create that signal, two of them automated: an LLM triage cron with no author filter, and the
# unauthenticated `POST /api/feedback` widget. So an outsider's issue body could become the prompt
# for a `--dangerously-skip-permissions` run under the owner's token.
#
# Two INDEPENDENT halves must hold, because neither implies the other: an outsider's issue can be
# labelled by a trusted bot (that was the feedback path), and a trusted author's issue can be
# labelled by anyone with triage. An unreadable answer is a REFUSAL, never a pass — a missed issue
# waits for the next tick, a wrongly-admitted one runs arbitrary work as the owner.
TRUSTED_ASSOCIATIONS="${TRUSTED_ASSOCIATIONS:-OWNER MEMBER COLLABORATOR}"
# Who may mint `agent:ready`. Deliberately NOT every bot: only automations whose input is not
# attacker-controlled. The feedback loop is absent on purpose (it files `needs-human` now).
AGENT_LABEL_TRUSTED_ACTORS="${AGENT_LABEL_TRUSTED_ACTORS:-$ASSIGNEE}"
# ...plus the pipeline's OWN bot, once it has one. This is not a widening of the gate: the runner
# re-applies `agent:ready` itself in two places — the stale-claim reaper, and the lane that returns
# an issue to the queue after the owner answers its Decision Comment — and MODE=phasefix files
# follow-up issues carrying it. Under the PAT those writes were the OWNER's and passed; under the
# app they are the bot's, so WITHOUT this every reaped issue, every answered Decision Comment and
# every phase-2 follow-up would be refused at dispatch and never run again. The standing granted is
# exactly the standing the credential already had — the outsider path this allowlist exists to
# close (a stranger's issue labelled by a non-allowlisted actor) is unchanged.
if [ "${GH_APP_IDENTITY_ACTIVE:-0}" = "1" ] && [ -n "${GH_APP_BOT_LOGIN:-}" ]; then
  case " $AGENT_LABEL_TRUSTED_ACTORS " in
    *" $GH_APP_BOT_LOGIN "*) ;;
    *) AGENT_LABEL_TRUSTED_ACTORS="$AGENT_LABEL_TRUSTED_ACTORS $GH_APP_BOT_LOGIN" ;;
  esac
fi
# Who may apply the CI-ROUTED auto-fix labels (`agent:depfix`, `agent:docfix`) — our own workflows,
# which act as `github-actions[bot]`. Kept apart from the human allowlist above on purpose: these
# two labels report a CI failure on an existing PR and grant no work, while `agent:ready` and
# `release:now` remain human-only. See `label_actor_trusted`.
AGENT_CI_LABEL_ACTORS="${AGENT_CI_LABEL_ACTORS:-github-actions[bot]}"

agent_token_scopes() {
  # Classic OAuth tokens advertise their scopes in a response header. Fine-grained PATs carry
  # per-resource permissions instead and send NO such header — so an empty result is the GOOD case.
  gh api -i user 2>/dev/null \
    | awk 'BEGIN{IGNORECASE=1} /^x-oauth-scopes:/{sub(/^[^:]*:[[:space:]]*/,""); print; exit}' \
    | tr -d '\r'
}

assert_agent_token_scoped() {
  # A `workflow`-scoped token lets the agent edit .github/workflows/ — i.e. edit its own gates.
  # Warns by default so configuring the PAT is not a prerequisite for the pipeline running at all;
  # set AGENT_REQUIRE_SCOPED_TOKEN=1 in config.env once the PAT is in place to make it fail closed.
  #
  # A GitHub App installation token has no OAuth scopes at all — its authority is the app's
  # declared permission set, verified at registration (contents/issues/pull_requests write,
  # metadata read, NO workflows). `gh api user` also 403s for an installation token, so the probe
  # below would read "no scopes" for the right reason but by accident; short-circuit so the log
  # says what is actually true rather than leaving a silent pass.
  if [ "${GH_APP_IDENTITY_ACTIVE:-0}" = "1" ]; then
    return 0
  fi
  local scopes; scopes="$(agent_token_scopes)"
  case ",${scopes// /}," in
    *,workflow,*)
      if [ "${AGENT_REQUIRE_SCOPED_TOKEN:-0}" = "1" ]; then
        log "FATAL: the pipeline token carries the 'workflow' scope — it can rewrite the workflows"
        log "       that gate merges and deploys. Configure AGENT_GH_TOKEN (a fine-grained PAT with"
        log "       Workflows: no access) in config.env. See docs/contribution-security.md."
        return 1
      fi
      log "WARNING: the pipeline token carries the 'workflow' scope — the agent can rewrite"
      log "         .github/workflows/. Set AGENT_GH_TOKEN in config.env (docs/contribution-security.md)."
      ;;
  esac
  return 0
}

author_trusted() {
  # author_trusted <number> -> 0 when the AUTHOR has standing in this repo.
  #
  # REST, deliberately — NOT `gh issue view --json authorAssociation`. That field does not exist in
  # gh's issue or PR JSON ("Unknown JSON field"), so the command fails, the association reads empty,
  # and this function then refuses — correctly for an unreadable answer, but it made EVERY issue
  # unreadable and idled the whole pipeline. The REST issues endpoint does expose
  # `author_association`, and it covers PRs too, because a PR is an issue.
  local n="$1" both assoc author
  both="$(gh api "repos/$SLUG/issues/$n" \
            --jq '"\(.author_association // "")\t\(.user.login // "")"' 2>/dev/null)"
  assoc="${both%%$'\t'*}"; author="${both#*$'\t'}"
  [ -n "$assoc" ] || { log "TRUST: #$n — author_association unreadable; refusing."; return 1; }
  # An issue the PIPELINE filed. MODE=phasefix's whole job is to file the follow-up issue a held
  # merge needs, and it labels it `agent:ready` — but a GitHub App is not a repo collaborator, so
  # its author_association is never one of OWNER/MEMBER/COLLABORATOR and every follow-up it filed
  # would sit unworkable forever. Nobody but this pipeline can author as this login.
  if [ -n "${GH_APP_BOT_LOGIN:-}" ] && [ "$author" = "$GH_APP_BOT_LOGIN" ]; then return 0; fi
  case " $TRUSTED_ASSOCIATIONS " in *" $assoc "*) return 0 ;; esac
  log "TRUST: #$n authored by $assoc — not eligible for autonomous work."
  return 1
}

label_actor_trusted() {
  # label_actor_trusted <number> <label> -> 0 when the LAST actor to apply <label> is allowlisted.
  # The timeline endpoint covers PRs too (a PR is an issue). We read the LAST `labeled` event for
  # that name: a label removed and re-added by someone else is theirs, not the original applier's.
  # `--slurp` + an EXTERNAL jq, not `--paginate --jq`: gh applies --jq to each PAGE separately, so
  # `| last` emits one login PER PAGE. Past 100 timeline events $actor becomes multi-line, the
  # case-match below fails, and the gate refuses — silently, and precisely on the long-lived,
  # heavily-discussed threads. (--slurp is rejected alongside --jq, hence the pipe.) --slurp yields
  # an array OF PAGES, so `.[][]` flattens it.
  local n="$1" label="$2" actor
  actor="$(gh api "repos/$SLUG/issues/$n/timeline" --paginate --slurp \
             -H "Accept: application/vnd.github+json" 2>/dev/null \
           | jq -r "[.[][] | select(.event==\"labeled\" and .label.name==\"${label}\")
                    | .actor.login] | last // empty" 2>/dev/null)"
  [ -n "$actor" ] || { log "TRUST: #$n — no readable '$label' labeler; refusing."; return 1; }
  case " $AGENT_LABEL_TRUSTED_ACTORS " in *" $actor "*) return 0 ;; esac
  # CI-ROUTED labels are the one place our own workflows are the legitimate applier. A router runs
  # `actions/github-script` with the default GITHUB_TOKEN, so the timeline actor is
  # `github-actions[bot]` — never a human, and never in the human allowlist. Refusing it made BOTH
  # auto-fix lanes dead on arrival: `agent:depfix` has been shipped since the Dependabot router
  # landed and has dispatched exactly ZERO times.
  #
  # This is deliberately narrow, and it is not the hole the allowlist exists to close. These two
  # labels grant nothing: they say "this EXISTING pull request has failing CI", not "build this".
  # `pr_is_upstream` still gates the work onto a branch inside this repo, and the labels that DO
  # grant privilege — `agent:ready`, `release:now` — stay human-only.
  #
  # Note what is NOT in that list: `pr_admissible` is `pr_is_upstream` + `label_actor_trusted`, and
  # `author_trusted` is deliberately absent from every PR lane (it runs once, on the ISSUE lane).
  # That is not an oversight to tidy up later — it is load-bearing. Dependabot PRs carry
  # `author_association: CONTRIBUTOR`, which is not in TRUSTED_ASSOCIATIONS (OWNER MEMBER
  # COLLABORATOR), so adding `author_trusted` here to make the gate look symmetrical would refuse
  # every Dependabot PR and put the depfix lane straight back into the dead-on-arrival state the
  # paragraph above describes. The control that matters on a PR lane is `pr_is_upstream`: getting a
  # branch into this repo at all already requires write access, which is a stronger statement than
  # any association string.
  case " $label " in
    " agent:depfix "|" agent:docfix ")
      case " $AGENT_CI_LABEL_ACTORS " in
        *" $actor "*) return 0 ;;
      esac
      ;;
  esac
  log "TRUST: #$n — '$label' applied by '$actor', not in AGENT_LABEL_TRUSTED_ACTORS."
  return 1
}

pr_is_upstream() {
  # pr_is_upstream <number> -> 0 when the PR's head branch lives in THIS repo, not a fork.
  # The PR lanes push to `origin/$branch` and merge; add_worktree resolves refs/remotes/origin/...,
  # so a fork PR would silently branch from main instead of carrying the contributor's code. That
  # is a correctness bug as much as a security one — refuse rather than do something surprising.
  local n="$1" head
  head="$(gh pr view "$n" --repo "$SLUG" --json headRepositoryOwner \
            --jq '.headRepositoryOwner.login // ""' 2>/dev/null)"
  [ -n "$head" ] || { log "TRUST: PR #$n — head repository unreadable; refusing."; return 1; }
  [ "$head" = "$OWNER" ] && return 0
  log "TRUST: PR #$n head is in fork '$head' — this pipeline only works upstream branches."
  return 1
}

pr_admissible() {
  # pr_admissible <number> <lane-label> -> both halves for a PR lane.
  pr_is_upstream "$1" && label_actor_trusted "$1" "$2"
}

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

closing_issue_for_pr() {
  # Issue that merging PR $1 would actually CLOSE — deliberately NARROWER than issue_for_pr.
  # The branch convention says which issue the work BELONGS to; it does NOT close anything. Those
  # two answers diverge exactly when a PR lands one phase of a multi-phase issue and omits the
  # closing keyword ON PURPOSE, which is the case the phase guard exists to bless, not to block:
  # #807 (phase 2a of #745, no closing keyword, `closingIssuesReferences` empty) was parked for a
  # day and told to "remove `Closes #745` from the PR body" — text that was never there, so the
  # instruction was impossible to follow and every tick re-parked it.
  local P="$1" I
  I="$(gh pr view "$P" --repo "$SLUG" --json closingIssuesReferences 2>/dev/null \
       | jq -r '[(.closingIssuesReferences // [])[] | .number] | (first // empty)')"
  [ -n "$I" ] && { echo "$I"; return 0; }
  # Body keyword only — GitHub populates the link from it, so this is a belt-and-braces catch for
  # an API blip, never a guess from the branch name.
  gh pr view "$P" --repo "$SLUG" --json body 2>/dev/null | jq -r '
      ((.body // "") | capture("(?i)(clos(e|es|ed)|fix(e[sd])?|resolv(e|es|ed))[[:space:]]+#(?<n>[0-9]+)")? | .n) // empty' 2>/dev/null
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

select_ready_issues() {
  # ALL ready issues in priority order: has agent:ready, not needs-human/agent:working/agent:blocked,
  # ordered: critical/high priority JUMP the line (regardless of milestone), then everyone
  # else by milestone number (7->12), then priority, then issue number.
  #
  # Returns the whole ordered list, not just the head, because the caller now has to walk it: an
  # issue can be label-eligible but fail the trust boundary, and stopping at the first one would
  # let a single inadmissible issue park the entire queue behind it.
  gh issue list --repo "$SLUG" --state open --limit 100 --label "agent:ready" \
    --json number,labels,milestone | jq -r '
    def prio: (.labels|map(.name)|map(select(startswith("priority:")))|.[0] // "priority:zzz")
      | {"priority:critical":0,"priority:high":1,"priority:medium":2,"priority:low":3}[.] // 4;
    def msnum: (.milestone.title // "Milestone 999" | capture("Milestone (?<n>[0-9]+)").n | tonumber);
    map(select((.labels|map(.name)) as $l
        | ($l|index("needs-human")|not) and ($l|index("agent:blocked")|not) and ($l|index("agent:working")|not)))
    | sort_by((if prio <= 1 then 0 else 1 end), msnum, prio, .number)
    | .[].number'
}

explain_empty_queue() {
  # Why the queue came back empty. "Pipeline idle" and "every candidate was excluded" are the SAME
  # log line otherwise, and the reaper's own header warns that silence looking identical to done is
  # how sixteen issues once accumulated invisibly. Purely diagnostic — reads only, changes nothing.
  local held
  held="$(gh issue list --repo "$SLUG" --state open --limit 100 --label "agent:ready" \
            --json number,labels 2>/dev/null \
          | jq -r '[.[] | . as $i | (.labels|map(.name)) as $l
                   | (["needs-human","agent:blocked","agent:working"] | map(select(. as $x | $l|index($x))))
                     as $blockers
                   | select($blockers|length > 0)
                   | "#\($i.number) (\($blockers|join(", ")))"] | join("; ")' 2>/dev/null)"
  [ -n "$held" ] && log "  ...$(echo "$held" | tr ';' '\n' | wc -l) agent:ready issue(s) excluded: $held"
  return 0
}

select_next_issue() {
  # The first ready issue that also clears the trust boundary (author standing + label provenance).
  local n
  for n in $(select_ready_issues); do
    if author_trusted "$n" && label_actor_trusted "$n" "agent:ready"; then
      echo "$n"; return 0
    fi
  done
  return 0
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
# The ASCII half — what detection keys on. Separate from the decorated marker so the decoration can
# change, or be mangled in transit, without silently breaking "has this been reviewed?".
CLAUDE_REVIEW_MARKER_TEXT="Claude adversarial review"
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
  # Match the ASCII PHRASE, not the whole marker. The marker opens with a non-BMP emoji (U+1F50E),
  # and a non-BMP character does not survive the round-trip through the agent that posts it — the
  # comment lands as four U+FFFD replacement characters. `startswith($m)` therefore never matched
  # what was actually written, so the agent could not see its OWN marker, concluded the review had
  # not happened, and posted again: five identical comments on PR #1273 before it was noticed.
  #
  # Same class of defect CLAUDE.md already documents for Selenium ("ChromeDriver send_keys throws
  # on non-BMP emoji — strip them first"). Anchoring on the ASCII phrase makes detection
  # independent of whether the decoration survives, and makes already-posted mangled markers count
  # rather than needing a cleanup pass.
  #
  # The phrase must still OPEN the comment. A bare `contains` would count any comment that merely
  # MENTIONS the review as review evidence — and two such comments are routine: a MODE=selfreview
  # escalation deliberately posts a Decision Comment INSTEAD of the marker, and this repo is
  # public, so anyone can write the phrase in prose. Either would clear the merge gate for a PR
  # that was never reviewed. Leading NON-LETTERS are stripped first, which is exactly the room the
  # decoration needs (`🔎`, its four U+FFFD, `#`, `**`, spaces) and no room at all for a word like
  # "the" in front of it.
  gh pr view "$1" --repo "$SLUG" --json comments 2>/dev/null \
    | jq -r --arg m "$CLAUDE_REVIEW_MARKER_TEXT" \
        '[(.comments // [])[] | select(((.body // "") | sub("^[^A-Za-z]*"; "")) | startswith($m))] | last | .createdAt // empty' 2>/dev/null
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
#     linked follow-up  -> route the PR to MODE=phasefix (an agent files+links the follow-up
#     itself — that is mechanical, not a human decision); only after MAX_PHASEFIX_ATTEMPTS
#     failed agent passes does it park to the owner;
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

# Phasefix budget: ONE counter per PR, bumped where the SPEND happens (the phasefix lane, at
# dispatch) and cleared the moment the guard passes. Counting holds instead of dispatches would not
# bound anything: a MODE=phasefix run that dies — or escalates — without handing the PR back to
# agent:working never reaches phase_guard_ok again, so the label (and the re-dispatch) would live
# forever on a counter that never moves.
phasefix_attempts() {  # $1=pr -> echoes the dispatch count (0 when unset/garbage)
  local c; c="$(cat "$BASE/state/phasefix-$1.count" 2>/dev/null || echo 0)"
  case "$c" in ''|*[!0-9]*) c=0 ;; esac
  echo "$c"
}
phasefix_bump()  { echo "$(( $(phasefix_attempts "$1") + 1 ))" > "$BASE/state/phasefix-$1.count"; }
phasefix_clear() { rm -f "$BASE/state/phasefix-$1.count"; }

phase_park_to_human() {  # $1=pr $2=issue $3=leftover $4=attempts — the agent lane gave up
  local P="$1" N="$2" leftover="$3" tries="$4"
  log "PHASE GUARD: PR #$P still fails after $tries phasefix attempt(s) — parking to human."
  if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: would park PR #$P + escalate issue #$N (phase guard, autofix exhausted)."; return 0; fi
  if ! gh pr view "$P" --repo "$SLUG" --json comments --jq '((.comments // [])[].body)' 2>/dev/null | grep -qF "phasefix exhausted"; then
    gh pr comment "$P" --repo "$SLUG" --body "$PHASE_GUARD_MARKER — **phasefix exhausted** after $tries attempt(s); this PR closes issue #$N whose later phase ($leftover) is still untracked. To clear: file + link the follow-up as \`Follow-up: #<n>\` (or remove \`Closes #$N\`), then re-label \`agent:working\`. Assigning @$ASSIGNEE." >/dev/null 2>&1
  fi
  gh pr edit "$P" --repo "$SLUG" --add-label needs-human --add-label agent:blocked --remove-label agent:working --remove-label agent:phasefix >/dev/null 2>&1
  gh issue edit "$N" --repo "$SLUG" --add-label needs-human --add-assignee "$ASSIGNEE" >/dev/null 2>&1
  gh pr edit "$P" --repo "$SLUG" --add-assignee "$ASSIGNEE" >/dev/null 2>&1
}

phase_guard_ok() {  # $1=pr -> 0 when merging is safe (guard off / nothing closed / scope complete)
  [ "$PHASE_GUARD" = "1" ] || return 0
  local P="$1" N leftover
  # closing_issue_for_pr, NOT issue_for_pr: the guard's whole premise is "merging this will close
  # the issue and silently drop the rest of its scope". A PR that closes nothing cannot drop
  # anything, however its branch happens to be named.
  N="$(closing_issue_for_pr "$P")"
  [ -n "$N" ] || return 0                      # closes nothing -> nothing can be dropped
  leftover="$(phase_leftover "$N")"
  [ -n "$leftover" ] || return 0
  phase_followup_linked "$P" "$N" && { phasefix_clear "$P"; return 0; }   # the remainder is already tracked

  # Unchecked boxes alone: warn once and let it merge — see the note above.
  case "$leftover" in
    boxes:*)
      if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: phase guard would warn on PR #$P (issue #$N, $leftover)."; return 0; fi
      if ! gh pr view "$P" --repo "$SLUG" --json comments --jq '((.comments // [])[].body)' 2>/dev/null | grep -qF "$PHASE_GUARD_MARKER"; then
        gh pr comment "$P" --repo "$SLUG" --body "$PHASE_GUARD_MARKER — heads up: issue #$N still has **$leftover** acceptance box(es). Merging anyway (unticked boxes are common here), but if any of that scope did NOT ship, file a follow-up issue with \`agent:ready\` and link it, or drop the \`Closes #$N\` from this PR." >/dev/null 2>&1
      fi
      return 0 ;;
  esac

  # Explicit later phase with nothing tracking it: the #548 failure mode. Filing + linking the
  # follow-up is MECHANICAL (the RUNBOOK's phased-work rule spells out exactly what to do), so it
  # is NOT a human decision — route the PR to MODE=phasefix and let an agent clear it. The owner
  # is only assigned after MAX_PHASEFIX_ATTEMPTS agent passes have failed to satisfy the guard.
  local pf_cnt
  pf_cnt="$(phasefix_attempts "$P")"
  if [ "$pf_cnt" -lt "$MAX_PHASEFIX_ATTEMPTS" ]; then
    log "PHASE GUARD: PR #$P closes issue #$N with a later phase ($leftover) untracked — queuing MODE=phasefix (attempt $((pf_cnt+1))/$MAX_PHASEFIX_ATTEMPTS) instead of parking."
    if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: would label PR #$P agent:phasefix."; return 1; fi
    if ! gh pr view "$P" --repo "$SLUG" --json comments --jq '((.comments // [])[].body)' 2>/dev/null | grep -qF "$PHASE_GUARD_MARKER"; then
      gh pr comment "$P" --repo "$SLUG" --body "$PHASE_GUARD_MARKER — **held before merge.** This PR closes issue #$N, whose body declares work beyond this PR ($leftover), and no follow-up issue is linked. Queued for **MODE=phasefix**: an agent will file + link the follow-up issue (or drop the closing keyword) and hand the PR back to the merge loop — no owner action needed unless that fails." >/dev/null 2>&1
    fi
    gh pr edit "$P" --repo "$SLUG" --add-label agent:phasefix --remove-label agent:working >/dev/null 2>&1
    return 1
  fi

  # The agent had its chances and the guard still fails — NOW it is the owner's.
  phase_park_to_human "$P" "$N" "$leftover" "$pf_cnt"
  return 1
}

# ---- MERGE EXECUTION: verify the OUTCOME, never the exit code (#1082) --------------------------
# `main` merges through a GitHub **merge queue**, so `gh pr merge --auto` means ENQUEUE — and it
# exits 0 for three very different outcomes: the PR entered the queue, the PR was already in it, or
# the PR holds a queue entry GitHub already evicted. Only the third is a deadlock, and it is silent:
# #1067 sat green-and-unmerged for 47h while every tick logged "Merging." and posted the same
# comment (561 of them), starving the single concurrency slot. The exit code proved nothing.
#
# So: ask for the merge, then read the STATE back. Merged or a live queue entry is progress;
# anything else is a stall, counted per PR, and after MERGE_STALE_TICKS the dangling entry is
# cleared (`--disable-auto`) and a fresh one requested — the manual recovery that worked, automated.
# The "merging" comment is keyed on the HEAD SHA, so one merge attempt can only ever produce one.
#
# A live entry is NOT the end of the question, because #1067's entry was live at every read: the
# timeline shows 154 added_to_merge_queue / 153 removed_from_merge_queue at exactly tick cadence —
# a merge-group check failed ~3 min after each enqueue, and the next tick re-enqueued. Read 20s
# after the request, that PR looked healthily QUEUED 154 times in a row. So the queue gets a
# BUDGET too: requests are counted per HEAD SHA, and a head the queue has refused
# MERGE_QUEUE_STUCK_TICKS times is reported as stuck however the entry looks right now.
MERGE_STALE_TICKS="${MERGE_STALE_TICKS:-3}"     # consecutive stalled ticks before forcing a re-enqueue
MERGE_QUEUE_STUCK_TICKS="${MERGE_QUEUE_STUCK_TICKS:-12}"  # merge requests one head may burn (~1h at 5-min ticks)
MERGE_STALL_CYCLES_MAX="${MERGE_STALL_CYCLES_MAX:-2}"  # disable-auto recovery cycles one head gets before parking
MERGE_VERIFY_TRIES="${MERGE_VERIFY_TRIES:-5}"   # state reads after asking for the merge
MERGE_VERIFY_SLEEP="${MERGE_VERIFY_SLEEP:-5}"   # seconds between them

classify_merge_state() {  # $1=PR state $2=merge-queue entry state $3=auto-merge armed (1/0)
  case "$1" in
    MERGED) echo merged; return 0 ;;
    CLOSED) echo closed; return 0 ;;
  esac
  # UNMERGEABLE is GitHub saying the merge-group run failed — the entry is on its way OUT of the
  # queue, so counting it as "the queue owns it" is the #1082 mistake in a new costume.
  [ "$2" = "UNMERGEABLE" ] && { echo unmergeable; return 0; }
  [ -n "$2" ] && { echo queued; return 0; }     # a live queue entry is the only proof of enqueue
  [ "$3" = "1" ] && { echo armed; return 0; }   # auto-merge set but no entry — progress, unproven
  echo unqueued                                 # unreadable state reads as NOT merged, never as merged
}

merge_queue_entry_state() {  # $1=pr -> the queue entry's state, empty when there is no live entry
  gh api graphql -f query='query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){
    pullRequest(number:$p){mergeQueueEntry{state}}}}' \
    -f o="$OWNER" -f n="$NAME" -F p="$1" \
    --jq '.data.repository.pullRequest.mergeQueueEntry.state // ""' 2>/dev/null
}

# Stall budget: ONE counter per PR, bumped on every tick whose merge request left the PR neither
# merged nor queued, cleared the moment either becomes true.
merge_stall_count() {  # $1=pr -> consecutive stalled ticks (0 when unset/garbage)
  local c; c="$(cat "$BASE/state/mergestall-$1.count" 2>/dev/null || echo 0)"
  case "$c" in ''|*[!0-9]*) c=0 ;; esac
  echo "$c"
}
merge_stall_bump()  { echo "$(( $(merge_stall_count "$1") + 1 ))" > "$BASE/state/mergestall-$1.count"; }
merge_stall_clear() { rm -f "$BASE/state/mergestall-$1.count"; }

# Queue budget: how many times this lane has asked GitHub to merge THIS head. A new push resets it
# (the queue deserves a fresh chance at new code); nothing else does except the PR actually landing.
merge_attempt_count() {  # $1=pr $2=head sha -> requests already made at this head (0 when it moved)
  local rec sha n
  rec="$(cat "$BASE/state/mergeattempt-$1" 2>/dev/null)"
  sha="${rec%% *}"; n="${rec##* }"
  [ -n "$sha" ] && [ "$sha" = "$2" ] || { echo 0; return 0; }
  case "$n" in ''|*[!0-9]*) n=0 ;; esac
  echo "$n"
}
merge_attempt_bump()  { printf '%s %s\n' "$2" "$(( $(merge_attempt_count "$1" "$2") + 1 ))" > "$BASE/state/mergeattempt-$1"; }
merge_attempt_clear() { rm -f "$BASE/state/mergeattempt-$1"; }

merge_comment_once() {  # $1=pr $2=head sha $3=body -> 0 when it commented, 1 when already said
  local P="$1" sha="$2" body="$3" f="$BASE/state/mergecomment-$1.sha"
  [ "$(cat "$f" 2>/dev/null)" = "$sha" ] && return 1
  printf '%s\n' "$sha" > "$f"
  [ "$DRY_RUN" = "1" ] || gh pr comment "$P" --repo "$SLUG" --body "$body" >/dev/null 2>&1
  return 0
}

# Stall-recovery cycles: how many times the disable-auto + re-enqueue recovery has fired for this
# PR. One recovery is the automated fix that worked for #1067; a SECOND exhaustion at the same
# head means the recovery does not work here, and repeating it forever is the #1120 pattern.
# HEAD-SCOPED, exactly like the queue budget: a push is new code and earns a fresh recovery
# allowance. A lifetime counter would park a PR on its FIRST stall after a fix landed, which is
# the opposite of what "the recovery does not work HERE" means. An old plain-integer record from
# before this change has no sha, so it reads as 0 — a stale file can only be lenient, never park.
merge_stall_cycle_count() {  # $1=pr $2=head sha -> recovery cycles already spent at this head
  local rec sha n
  rec="$(cat "$BASE/state/mergestallcycle-$1.count" 2>/dev/null)"
  sha="${rec%% *}"; n="${rec##* }"
  [ -n "$sha" ] && [ "$sha" = "$2" ] || { echo 0; return 0; }
  case "$n" in ''|*[!0-9]*) n=0 ;; esac
  echo "$n"
}
merge_stall_cycle_bump()  { printf '%s %s\n' "$2" "$(( $(merge_stall_cycle_count "$1" "$2") + 1 ))" > "$BASE/state/mergestallcycle-$1.count"; }
merge_stall_cycle_clear() { rm -f "$BASE/state/mergestallcycle-$1.count"; }

# Best-effort link to the failing merge_group run: the queue builds on a temporary
# gh-readonly-queue/main/pr-<PR>-* branch, so recent merge_group runs carrying "pr-<PR>-" in their
# head branch are this PR's queue builds. Unreadable -> empty, and the comment says where to look.
failing_merge_group_run() {  # $1=pr -> URL or ""
  gh run list --repo "$SLUG" --event merge_group --limit 15 \
    --json url,conclusion,headBranch \
    --jq "[.[] | select(.headBranch | contains(\"pr-$1-\")) | select(.conclusion==\"failure\")] | (first // {}) | .url // empty" \
    2>/dev/null
}

# One park comment per head, on its OWN key file — mergecomment-<pr>.sha already holds this head's
# "merging" comment key, so sharing it would silently swallow the Decision Comment.
merge_park_once() {  # $1=pr $2=head sha $3=body
  local P="$1" sha="$2" body="$3" f="$BASE/state/mergepark-$1.sha"
  [ "$(cat "$f" 2>/dev/null)" = "$sha" ] && return 1
  printf '%s\n' "$sha" > "$f"
  [ "$DRY_RUN" = "1" ] || gh pr comment "$P" --repo "$SLUG" --body "$body" >/dev/null 2>&1
  return 0
}

# PARK a PR the merge queue keeps refusing. Detection without a state change was the #1120 failure
# (45 requests) and #1067 before it (154): the PR stayed agent:working, so every next tick re-fed
# it to the queue and the lane starved everything behind it. Parking makes the detected state REAL:
#   draft FIRST (a draft cannot hold auto-merge or a queue entry, so a concurrently-running tick
#   that re-arms fails closed instead of undoing the park), then disable auto-merge, then the
#   labels, then ONE Decision Comment per head with the failing merge_group run when readable.
# The owner's reply routes through the normal answer lane (route_owner_answer), which un-drafts,
# clears the merge budgets, and hands the PR to the revise lane.
park_merge_stuck() {  # $1=pr $2=head sha $3=one-line reason for the log/comment
  local P="$1" SHA="$2" reason="$3" ISS runlink asked
  asked="$(merge_attempt_count "$P" "$SHA")"
  log "MERGE: PARKING PR #$P ($reason) — drafting, disabling auto-merge, labeling needs-human. The owner's answer (or a new head after a fix) un-parks it."
  if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: would park PR #$P ($reason)."; return 0; fi
  gh pr ready "$P" --repo "$SLUG" --undo >/dev/null 2>&1
  gh pr merge "$P" --repo "$SLUG" --disable-auto >/dev/null 2>&1
  gh pr edit "$P" --repo "$SLUG" \
    --add-label "needs-human" --add-label "agent:blocked" --add-label "agent:merge-parked" \
    --remove-label "agent:working" >/dev/null 2>&1
  gh pr edit "$P" --repo "$SLUG" --add-assignee "$ASSIGNEE" >/dev/null 2>&1
  ISS="$(issue_for_pr "$P")"
  # Mirror the FULL hold, not just needs-human: route_owner_answer un-parks by adding agent:working
  # and removing needs-human + agent:blocked, so an issue left reading agent:working while its PR is
  # parked makes the two threads disagree about whether this work is in flight.
  [ -n "$ISS" ] && gh issue edit "$ISS" --repo "$SLUG" \
    --add-label "needs-human" --add-label "agent:blocked" \
    --remove-label "agent:working" >/dev/null 2>&1
  runlink="$(failing_merge_group_run "$P")"
  merge_park_once "$P" "$SHA" "🛑 **Human decision needed** — the merge queue keeps rejecting this PR.

The queue has taken and dropped head \`${SHA:0:8}\` **$asked** times ($reason). A \`merge_group\` check is failing, so re-enqueueing cannot help — the lane has PARKED this PR (draft + auto-merge disabled) instead of burning more attempts.

Failing merge_group run: ${runlink:-not readable — check the merge_group runs on the Actions tab}

### 1. How should we proceed?
- **A. Re-enqueue this head as-is** — I fixed the failing merge_group check.
- **B. Rebase onto latest main and re-run the gate** — a queue failure at one head usually means main moved underneath it.  ✅ *recommended*
- **C. Close this PR** — I'll handle it manually.

**My recommendation: \`1B\`.** A merge_group failure at a head that main has moved past clears on a rebase far more often than it clears on a retry.

Reply with the question number and letter — e.g. \`1B\` — or \`ok\` to take the recommendation. (A bare \`B\` is NOT recognised as an answer; the answer lane needs the number.)"
  # `escalated`, not `dispatched` — the vocabulary the other human handoffs already use
  # (depfix/docfix/phasefix/fix_exhausted). A park that reported itself as a dispatch would show up
  # in the tick_outcome dashboards as productive work, which is exactly the "why nobody noticed"
  # half of #1082 and #1120.
  TICK_OUTCOME="escalated"; TICK_REASON="merge_parked"; TICK_PR="$P"
}

merge_pr() {  # $1=pr $2=comment body -> 0 when the merge landed or is genuinely queued
  local P="$1" BODY="$2" SHA PST QST CLS tries stall asked
  SHA="$(gh pr view "$P" --repo "$SLUG" --json headRefOid --jq .headRefOid 2>/dev/null)"
  # An unreadable head still needs a stable comment key, or "once per attempt" becomes "every tick".
  [ -z "$SHA" ] && SHA="unknown"

  # BUDGET BEFORE ENQUEUE. The old order re-armed auto-merge first and checked the budget after,
  # so a head past its budget was re-fed to the queue anyway — exceeding MERGE_QUEUE_STUCK_TICKS
  # never actually stopped the re-enqueue (#1120 reached 45 requests against a budget of 12).
  # This check sits ABOVE the DRY_RUN return (the SHA fetch is a read; park_merge_stuck is
  # DRY_RUN-guarded itself) so a dry tick against a synthetic budget file proves the park path.
  if [ "$(merge_attempt_count "$P" "$SHA")" -ge "$MERGE_QUEUE_STUCK_TICKS" ]; then
    park_merge_stuck "$P" "$SHA" "queue budget spent: $(merge_attempt_count "$P" "$SHA") requests at this head"
    return 1
  fi
  if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: would merge PR #$P."; return 0; fi

  stall="$(merge_stall_count "$P")"
  if [ "$stall" -ge "$MERGE_STALE_TICKS" ]; then
    # The recovery gets a budget of its own: cycle 1 is the automated fix that worked for #1067;
    # a repeat exhaustion at the same head means the recovery does not work HERE — park, don't loop.
    merge_stall_cycle_bump "$P" "$SHA"
    if [ "$(merge_stall_cycle_count "$P" "$SHA")" -gt "$MERGE_STALL_CYCLES_MAX" ]; then
      park_merge_stuck "$P" "$SHA" "auto-merge keeps arming without a queue entry after $(merge_stall_cycle_count "$P" "$SHA") disable-auto recovery cycles"
      return 1
    fi
    log "MERGE: PR #$P — stalled for $stall consecutive tick(s) with no live merge-queue entry; clearing the dangling auto-merge/queue state (--disable-auto) before re-enqueueing (recovery cycle $(merge_stall_cycle_count "$P" "$SHA")/$MERGE_STALL_CYCLES_MAX)."
    gh pr merge "$P" --repo "$SLUG" --disable-auto >/dev/null 2>&1
    merge_stall_clear "$P"
  fi

  # Always --repo-scoped: $BASE is not a git checkout, so a bare PR number would resolve against
  # whatever repo the cwd happens to be — the wrong repo, or none at all.
  gh pr merge --auto "$P" --repo "$SLUG" >/dev/null 2>&1
  merge_attempt_bump "$P" "$SHA"
  asked="$(merge_attempt_count "$P" "$SHA")"

  tries=0
  while : ; do
    PST="$(gh pr view "$P" --repo "$SLUG" --json state,autoMergeRequest \
             --jq '.state + "|" + (if .autoMergeRequest then "1" else "0" end)' 2>/dev/null)"
    QST="$(merge_queue_entry_state "$P")"
    CLS="$(classify_merge_state "${PST%%|*}" "$QST" "${PST##*|}")"
    # `armed` is deliberately NOT terminal: an entry usually appears a beat after the request, and
    # settling on `armed` early would count a healthy enqueue as a stall.
    case "$CLS" in merged|closed|queued|unmergeable) break ;; esac
    tries=$(( tries + 1 ))
    [ "$tries" -ge "$MERGE_VERIFY_TRIES" ] && break
    sleep "$MERGE_VERIFY_SLEEP"
  done

  case "$CLS" in
    merged)
      merge_stall_clear "$P"; merge_attempt_clear "$P"; merge_stall_cycle_clear "$P"
      rm -f "$BASE/state/mergepark-$P.sha"
      log "MERGE: PR #$P is MERGED."
      merge_comment_once "$P" "$SHA" "$BODY"
      return 0 ;;
    queued)
      merge_stall_clear "$P"
      # An entry that is live NOW proves this request was taken; it does NOT prove the queue is
      # making progress. #1067 was re-enqueued and evicted 154 times at this exact cadence, and
      # every single read looked like this line. Spending the budget is the tell — and spending
      # it now PARKS the PR instead of logging and retrying next tick (the #1120 gap).
      if [ "$asked" -ge "$MERGE_QUEUE_STUCK_TICKS" ]; then
        park_merge_stuck "$P" "$SHA" "queue took and dropped this head $asked times (entry: ${QST:-unknown})"
        return 1
      fi
      log "MERGE: PR #$P is WAITING IN THE MERGE QUEUE (entry: ${QST:-unknown}, request $asked/$MERGE_QUEUE_STUCK_TICKS at this head) — the queue owns it now, no further action this tick."
      merge_comment_once "$P" "$SHA" "$BODY"
      return 0 ;;
    unmergeable)
      # GitHub has already judged this head: the merge-group run failed. Re-requesting cannot
      # help, so this parks IMMEDIATELY — the code always said "the lane will not re-request its
      # way out of this", and now the state changes to match the words.
      merge_stall_clear "$P"
      park_merge_stuck "$P" "$SHA" "merge-queue entry UNMERGEABLE at request $asked — a merge_group check failed"
      return 1 ;;
    armed)
      # Auto-merge is set but nothing is queued yet. Normal for a few seconds — and also exactly
      # what a dangling entry looks like — so it counts toward the stall budget either way.
      merge_stall_bump "$P"
      log "MERGE: PR #$P — merge REQUESTED (auto-merge armed), but no merge-queue entry yet (stall $(merge_stall_count "$P")/$MERGE_STALE_TICKS)."
      merge_comment_once "$P" "$SHA" "$BODY"
      return 0 ;;
    closed)
      merge_stall_clear "$P"; merge_attempt_clear "$P"; merge_stall_cycle_clear "$P"
      rm -f "$BASE/state/mergepark-$P.sha"
      log "MERGE: PR #$P is CLOSED without merging — not re-requesting."
      TICK_OUTCOME="skipped"; TICK_REASON="merge_pr_closed"
      return 1 ;;
    *)
      # The #1082 state. Report it as a FAILED tick, not a dispatched one: "why nobody noticed" was
      # half the incident, and a tick that asked for a merge and got nothing has not advanced.
      merge_stall_bump "$P"
      log "MERGE: PR #$P — merge requested but the PR is NEITHER merged NOR in the merge queue (stall $(merge_stall_count "$P")/$MERGE_STALE_TICKS); forcing a re-enqueue once the budget is reached."
      TICK_OUTCOME="failed"; TICK_REASON="merge_not_taken"
      return 1 ;;
  esac
}
# ---- end merge execution ----------------------------------------------------------------------

worktree_has_unsaved_work() {  # $1=path -> 0 if it holds work that is NOT on the remote
  # Two ways an agent's work can exist only here: uncommitted changes, and commits it never pushed
  # (a run killed between `git commit` and `git push`). Either one makes the directory the ONLY
  # copy, so removing it destroys work. Anything unreadable counts as unsafe.
  local wt="$1" br
  [ -d "$wt" ] || return 1
  git -C "$wt" rev-parse --git-dir >/dev/null 2>&1 || return 0     # unreadable -> treat as unsafe
  [ -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ] && return 0
  br="$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null)" || return 0
  [ -z "$br" ] || [ "$br" = "HEAD" ] && return 0                    # detached -> cannot compare
  if git -C "$wt" rev-parse --verify --quiet "refs/remotes/origin/$br" >/dev/null 2>&1; then
    [ -n "$(git -C "$wt" log --oneline "origin/$br..HEAD" 2>/dev/null)" ] && return 0
    return 1
  fi
  # No remote branch at all: unpushed by definition unless it carries nothing beyond main.
  [ -n "$(git -C "$wt" log --oneline "origin/main..HEAD" 2>/dev/null)" ] && return 0
  return 1
}

branch_lock_free() {  # $1=branch -> 0 when NO live tick holds the claim
  # claim_branch() flocks this same file for the life of a tick. Testing it non-blockingly on a
  # SEPARATE fd is how cleanup avoids racing a run that is mid-flight.
  local lf="$BASE/locks/br-$(echo "$1" | tr '/' '_').lock"
  [ -f "$lf" ] || return 0
  ( exec 9>"$lf"; flock -n 9 ) 2>/dev/null
}

release_worktree() {  # $1=path -> remove it unless it is in use or holds unsaved work
  local wt="$1" br
  [ -n "$wt" ] && [ -d "$wt" ] || return 0
  case "$wt" in "$WORKROOT"/*) ;; *) return 0 ;; esac   # never touch anything outside work/
  br="$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  if [ -n "$br" ] && ! branch_lock_free "$br"; then return 0; fi
  # WORKTREE_MERGED=1 means GitHub confirmed the branch's PR merged, so unpushed-looking commits are
  # the squash, not lost work. An UNCOMMITTED tree is still kept either way — a merged PR says
  # nothing about edits made after it landed.
  if [ -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ]; then
    log "worktree $wt kept: uncommitted changes."
    return 0
  fi
  if [ "${WORKTREE_MERGED:-0}" != "1" ] && worktree_has_unsaved_work "$wt"; then
    log "worktree $wt kept: it holds unpushed work and no merged PR."
    return 0
  fi
  git -C "$REPO" worktree remove --force "$wt" >/dev/null 2>&1 || rm -rf "$wt"
  git -C "$REPO" worktree prune >/dev/null 2>&1
  return 0
}

sweep_stale_worktrees() {
  # The box accumulated 255 worktrees / 40G before this existed: add_worktree only ever cleaned the
  # ONE branch it was about to recreate, and nothing swept the rest. A worktree is disposable the
  # moment its work is on the remote — add_worktree rebuilds it from origin in seconds.
  #
  # Rate-limited by a stamp file: this stats every worktree, and the tick runs every 5 minutes.
  local stamp="$BASE/locks/.worktree-sweep" now age open removed=0 wt br
  now="$(date +%s)"
  if [ -f "$stamp" ]; then
    age=$(( now - $(stat -c %Y "$stamp" 2>/dev/null || echo 0) ))
    [ "$age" -lt "${WORKTREE_SWEEP_INTERVAL:-3600}" ] && return 0
  fi
  : > "$stamp"
  # One API call, not one per worktree. An unreadable answer means we keep everything: deleting on
  # the assumption that a PR is closed, when we simply could not ask, is the wrong way to be wrong.
  open="$(gh pr list --repo "$SLUG" --state open --limit 200 --json headRefName --jq '.[].headRefName' 2>/dev/null)" || return 0
  [ -z "$open" ] && ! gh pr list --repo "$SLUG" --state open --limit 1 >/dev/null 2>&1 && return 0
  # MERGED branches must be asked about separately, because local git CANNOT tell they landed. The
  # repo squash-merges and auto-deletes the branch, so a merged worktree has no `origin/<branch>`
  # and its commits never appear on main — `origin/main..HEAD` stays non-empty forever and the
  # unsaved-work check reads shipped work as unsaved. Measured: 198 of 255 worktrees here.
  merged="$(gh pr list --repo "$SLUG" --state merged --limit 400 --json headRefName --jq '.[].headRefName' 2>/dev/null || true)"
  # Iterate GIT'S OWN inventory, not a glob. A branch name contains slashes, so a worktree lands at
  # work/feature/claude-issue-123 — two levels down. A `"$WORKROOT"/*/` glob sees only the
  # intermediate `work/feature/` directory, which is not a worktree, so the first version of this
  # swept 3 entries instead of 255 and reported success. Ask the source of truth.
  while IFS= read -r wt; do
    [ -n "$wt" ] && [ -d "$wt" ] || continue
    case "$wt" in "$WORKROOT"/*) ;; *) continue ;; esac
    # Grace period: a run that just finished may still be opening its PR.
    [ $(( now - $(stat -c %Y "$wt" 2>/dev/null || echo "$now") )) -lt "${WORKTREE_GRACE_SECONDS:-7200}" ] && continue
    br="$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    [ -n "$br" ] && printf '%s\n' "$open" | grep -qxF "$br" && continue   # still has an open PR
    if [ -n "$br" ] && printf '%s\n' "$merged" | grep -qxF "$br"; then
      # GitHub says this landed, so the local divergence is the squash, not lost work.
      WORKTREE_MERGED=1 release_worktree "$wt"
    else
      release_worktree "$wt"
    fi
    [ ! -d "$wt" ] && removed=$((removed+1))
  done < <(git -C "$REPO" worktree list --porcelain | awk '/^worktree /{print $2}')
  [ "$removed" -gt 0 ] && log "worktree sweep: removed $removed stale worktree(s)."
  return 0
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
    # Origin ref missing, so there are three cases and only one of them wants `-b`.
    if git -C "$REPO" show-ref --verify --quiet "refs/heads/$branch"; then
      local tip
      tip="$(git -C "$REPO" rev-parse --verify "$branch" 2>/dev/null)" || tip=""
      if [ -n "$tip" ] && git -C "$REPO" merge-base --is-ancestor "$tip" "$base" 2>/dev/null; then
        # Stale ref carrying nothing: delete it so `-b` can recreate it cleanly.
        log "add_worktree: deleting stale local $branch (tip on $base) before creating worktree." >&2
        git -C "$REPO" branch -D "$branch" >/dev/null 2>&1 || true
      else
        # The branch has UNPUSHED WORK. Deleting it would throw that away, but `-b` refuses to
        # recreate an existing branch — so the tick failed here every time and the issue could never
        # be worked (feature/claude-issue-744 sat with 3 unique commits and a missing directory,
        # failing on every tick until the reaper parked it). Attach the existing branch instead.
        log "add_worktree: reattaching existing $branch (has commits not on $base) to a fresh worktree." >&2
        git -C "$REPO" worktree add "$wt" "$branch" >/dev/null 2>&1
      fi
    fi
    # Only create the branch if the reattach above didn't already produce the worktree.
    [ -d "$wt" ] || git -C "$REPO" worktree add -b "$branch" "$wt" "$base" >/dev/null 2>&1
  fi
  if [ ! -d "$wt" ]; then
    log "add_worktree: FAILED to create $wt (branch=$branch base=$base). Check git worktree list." >&2
    return 1
  fi
  # Recorded so the EXIT trap can release it on EVERY path, including a crash or a kill. Set here
  # rather than at the nine call sites, for the same reason the worktree guard lives in run_lane().
  TICK_WORKTREE="$wt"
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
# EVERY worktree below is cut from `origin/main`, so this fetch is what makes "latest main" true.
# Its result used to be discarded: a failed fetch (network blip, expired credential) left the tick
# branching from whatever `origin/main` happened to say last time — silently, and looking identical
# to a healthy run. Cron retries in 5 minutes, so refusing is cheaper than a day of PRs cut from a
# stale base.
if ! git -C "$REPO" fetch origin --prune >/dev/null 2>&1; then
  log "git fetch origin FAILED — refusing to dispatch from a possibly stale origin/main. Retrying next tick."
  TICK_OUTCOME="error"; TICK_REASON="fetch_failed"
  exit 1
fi

sweep_stale_worktrees

# ---- STALE-CLAIM REAPER: return abandoned agent:working issues to the queue ----------------
# `agent:working` is stamped the moment a run STARTS, and select_next_issue excludes it. So any run
# that dies before opening a PR — a killed worktree, a 400 from the proxy, a timeout, a reboot —
# parks its issue in a state nothing ever leaves. There is no other exit from that state: on
# 2026-07-29/30 sixteen issues accumulated there and the queue drained to zero while every tick
# cheerfully logged "Pipeline idle" with both lanes green. Silence looked identical to done.
#
# An issue is reaped only when ALL of these hold, so live work is never yanked out from under a run:
#   - no OPEN PR is linked to it (pr_for_issue) — a PR means the run got far enough to hand off
#   - its branch claim lock is FREE — a concurrent slot working it holds that flock
#   - it has not been touched for STALE_CLAIM_MINUTES (label edits count as touches)
# Bounded: after STALE_CLAIM_MAX_REQUEUES round trips the issue is parked for a human instead of
# cycling forever on whatever keeps killing it.
STALE_CLAIM_MINUTES="${STALE_CLAIM_MINUTES:-120}"
STALE_CLAIM_MAX_REQUEUES="${STALE_CLAIM_MAX_REQUEUES:-3}"
reap_stale_claims() {
  local now cutoff n upd pr lockf cnt drop
  now="$(date +%s)"
  cutoff=$(( now - STALE_CLAIM_MINUTES * 60 ))
  for n in $(gh issue list --repo "$SLUG" --state open --limit 100 --label "agent:working" \
               --json number,labels \
               --jq '.[] | select((.labels|map(.name)) | (index("needs-human")|not) and (index("agent:blocked")|not)) | .number' 2>/dev/null); do
    upd="$(gh issue view "$n" --repo "$SLUG" --json updatedAt --jq .updatedAt 2>/dev/null)"
    [ -n "$upd" ] || continue
    [ "$(epoch "$upd")" -lt "$cutoff" ] || continue
    pr="$(pr_for_issue "$n" 2>/dev/null)"
    # A PR means the run handed off — clear any requeue history so a future stall starts from zero
    # rather than inheriting a count from a problem that has since been solved.
    [ -n "$pr" ] && { rm -f "$BASE/state/requeue-$n.count"; continue; }
    # A slot actively working this issue holds the branch claim. Probe it on a throwaway fd so we
    # never disturb this tick's own claim on fd 10. Both branch prefixes the pipeline creates.
    for lockf in "$BASE/locks/br-feature_claude-issue-$n.lock" "$BASE/locks/br-fix_claude-issue-$n.lock"; do
      [ -e "$lockf" ] || continue
      exec 9>"$lockf"
      if ! flock -n 9; then exec 9>&-; continue 2; fi
      exec 9>&-
    done
    cnt="$(cat "$BASE/state/requeue-$n.count" 2>/dev/null || echo 0)"
    cnt=$((cnt + 1))
    if [ "$cnt" -gt "$STALE_CLAIM_MAX_REQUEUES" ]; then
      log "REAPER: issue #$n abandoned $((cnt - 1))x with no PR — parking for a human."
      gh issue edit "$n" --repo "$SLUG" --add-label needs-human --add-label agent:blocked \
        --add-assignee "$ASSIGNEE" --remove-label agent:working >/dev/null 2>&1
      gh issue comment "$n" --repo "$SLUG" --body "🧹 Pipeline reaper: this issue was claimed \`agent:working\` and abandoned without a PR $((cnt - 1)) times (limit \`STALE_CLAIM_MAX_REQUEUES\`). Something is killing the run before it can hand off, so re-queueing it again would just burn slots. Parked for a human — check \`$LOGDIR\` for the failing run." >/dev/null 2>&1
      posthog_capture "issue_reaped" "agent-pipeline" "{\"issue_number\":$n,\"action\":\"parked\",\"requeue_count\":$((cnt - 1))}" 2>/dev/null || true
      continue
    fi
    # Drop the stale ai:* routing labels too: they describe the lane of the run that DIED, and
    # leaving them makes the next run's labels a lie about which provider did the work. Read the
    # list OFF THE ISSUE instead of hardcoding it — `gh issue edit` rejects the ENTIRE edit if any
    # named label is unknown to the repo, so one drifted name in a static list silently undoes the
    # whole requeue. That is not hypothetical: a hardcoded 'ai:claude' (the real label is
    # 'ai:claude-subscription') failed exactly this way in testing, leaving the issue claimed.
    drop="$(gh issue view "$n" --repo "$SLUG" --json labels \
              --jq '[.labels[].name | select(startswith("ai:"))] | join(",")' 2>/dev/null)"
    if ! gh issue edit "$n" --repo "$SLUG" --add-label agent:ready --remove-label agent:working \
           ${drop:+--remove-label "$drop"} >/dev/null 2>&1; then
      log "REAPER: label update FAILED for issue #$n — leaving the claim in place for the next tick."
      continue
    fi
    # Count only a requeue that actually landed, so a failing edit can't exhaust the retry budget.
    echo "$cnt" > "$BASE/state/requeue-$n.count"
    log "REAPER: issue #$n stale in agent:working since $upd with no PR — returned to agent:ready (requeue $cnt/$STALE_CLAIM_MAX_REQUEUES)."
    posthog_capture "issue_reaped" "agent-pipeline" "{\"issue_number\":$n,\"action\":\"requeued\",\"requeue_count\":$cnt}" 2>/dev/null || true
  done
}
[ "$DRY_RUN" = "1" ] || reap_stale_claims

# Credential check runs here, not in the guards block at the top: the helpers it needs are defined
# below that block, and this is the last point before any lane can act.
if ! assert_agent_token_scoped; then
  TICK_OUTCOME="skipped"; TICK_REASON="token_scope_refused"; exit 0
fi

# ---- PRIORITY LANE: Dependabot CI failures (labeled agent:depfix by the router workflow) ----
# Handled before roadmap work so dependency PRs get unblocked fast. One Claude call per tick.
DEPFIX="$(gh pr list --repo "$SLUG" --state open --label "agent:depfix" \
  --json number,headRefName,labels \
  | jq -r 'map(select((.labels|map(.name))|index("needs-human")|not))|.[0]//empty|@json')"
if [ -n "$DEPFIX" ] && ! pr_admissible "$(echo "$DEPFIX" | jq -r .number)" "agent:depfix"; then
  DEPFIX=""   # refused by the trust boundary — fall through to the other lanes this tick
fi
if [ -n "$DEPFIX" ]; then
  DPR="$(echo "$DEPFIX" | jq -r .number)"
  DBR="$(echo "$DEPFIX" | jq -r .headRefName)"
  # DISPATCHED RUNS via the ledger, not co-authored commits: the commit grep was free for any run
  # that died before committing — exactly the run a budget exists to stop repeating.
  CLAUDE_TRIES="$(ledger_count pr "$DPR" depfix)"
  if [ "${CLAUDE_TRIES:-0}" -ge 3 ]; then
    log "Dependabot PR #$DPR still failing after $CLAUDE_TRIES depfix runs — escalating."
    TICK_OUTCOME="escalated"; TICK_REASON="depfix_exhausted"; TICK_PR="$DPR"; TICK_BRANCH="$DBR"
    if [ "$DRY_RUN" != "1" ]; then
      gh pr edit "$DPR" --repo "$SLUG" --add-label needs-human --remove-label agent:depfix >/dev/null 2>&1
      gh issue comment "$DPR" --repo "$SLUG" --body "🚧 Claude couldn't fix CI after $CLAUDE_TRIES attempts on this Dependabot PR. Assigning @$ASSIGNEE." >/dev/null 2>&1
      gh pr edit "$DPR" --repo "$SLUG" --add-assignee "$ASSIGNEE" >/dev/null 2>&1
    fi
    exit 0
  fi
  log "Dependabot PR #$DPR failing — invoking depfix (priority lane, try $((CLAUDE_TRIES+1)))."
  TICK_OUTCOME="dispatched"; TICK_REASON="mode_depfix"; TICK_MODE="depfix"; TICK_PR="$DPR"; TICK_BRANCH="$DBR"
  if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: would run MODE=depfix for #$DPR ($DBR)."; exit 0; fi
  if ! claim_branch "$DBR"; then
    log "Dependabot PR #$DPR already claimed by another slot — moving on."
  else
    ledger_charge pr "$DPR" depfix >/dev/null
    WT="$(add_worktree "$DBR" origin/main)"
    export MODE=depfix PR="$DPR" BRANCH="$DBR" WORKTREE="$WT"
    run_claude "$WT" "Read $RUNBOOK and follow MODE=depfix. PR=$DPR BRANCH=$DBR."
    exit 0
  fi
fi

# ---- PRIORITY LANE: Docstring & Lint Gate failures (labeled agent:docfix by the router) ----
# Runs right after depfix and before roadmap work: a lint failure blocks a PR that is otherwise
# finished, and it is the class of defect an agent should never need a human for. Same escalation
# budget as depfix — three Claude attempts, then it becomes a human's problem rather than a loop.
DOCFIX="$(gh pr list --repo "$SLUG" --state open --label "agent:docfix" \
  --json number,headRefName,labels \
  | jq -r 'map(select((.labels|map(.name))|index("needs-human")|not))|.[0]//empty|@json')"
if [ -n "$DOCFIX" ] && ! pr_admissible "$(echo "$DOCFIX" | jq -r .number)" "agent:docfix"; then
  DOCFIX=""   # refused by the trust boundary — fall through to the other lanes this tick
fi
if [ -n "$DOCFIX" ]; then
  XPR="$(echo "$DOCFIX" | jq -r .number)"
  XBR="$(echo "$DOCFIX" | jq -r .headRefName)"
  # DISPATCHED RUNS via the ledger, not co-authored commits (same reasoning as depfix above).
  CLAUDE_TRIES="$(ledger_count pr "$XPR" docfix)"
  if [ "${CLAUDE_TRIES:-0}" -ge 3 ]; then
    log "PR #$XPR still failing the lint gate after $CLAUDE_TRIES docfix runs — escalating."
    TICK_OUTCOME="escalated"; TICK_REASON="docfix_exhausted"; TICK_PR="$XPR"; TICK_BRANCH="$XBR"
    if [ "$DRY_RUN" != "1" ]; then
      gh pr edit "$XPR" --repo "$SLUG" --add-label needs-human --remove-label agent:docfix >/dev/null 2>&1
      gh issue comment "$XPR" --repo "$SLUG" --body "🚧 Claude couldn't clear the Docstring & Lint Gate after $CLAUDE_TRIES attempts. The standard is \`docs/docstring-standard.md\`; assigning @$ASSIGNEE." >/dev/null 2>&1
      gh pr edit "$XPR" --repo "$SLUG" --add-assignee "$ASSIGNEE" >/dev/null 2>&1
    fi
    exit 0
  fi
  log "PR #$XPR failing the lint gate — invoking docfix (priority lane, try $((CLAUDE_TRIES+1)))."
  TICK_OUTCOME="dispatched"; TICK_REASON="mode_docfix"; TICK_MODE="docfix"; TICK_PR="$XPR"; TICK_BRANCH="$XBR"
  if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: would run MODE=docfix for #$XPR ($XBR)."; exit 0; fi
  if ! claim_branch "$XBR"; then
    log "PR #$XPR already claimed by another slot — moving on."
  else
    ledger_charge pr "$XPR" docfix >/dev/null
    WT="$(add_worktree "$XBR" origin/main)"
    export MODE=docfix PR="$XPR" BRANCH="$XBR" WORKTREE="$WT"
    run_claude "$WT" "Read $RUNBOOK and follow MODE=docfix. PR=$XPR BRANCH=$XBR."
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
      # UN-DRAFT FIRST. Escalations (fix-exhausted, merge-park) convert the PR to a draft, and a
      # draft can neither enter the merge queue nor hold auto-merge — #1236 sat "CLEAN with 21
      # green checks" for hours because an earlier answer pass removed the labels but never ran
      # `gh pr ready`, and nothing else ever does. Order matters (ready before labels): the
      # moment labels flip the pipeline may act on the PR, and it must act on a READY one.
      gh pr ready "$TPR" --repo "$SLUG" >/dev/null 2>&1
      gh pr edit "$TPR" --repo "$SLUG" --add-label agent:revise \
        --remove-label needs-human --remove-label agent:blocked \
        --remove-label agent:merge-parked >/dev/null 2>&1
      # Mirror onto the issue so it stops reading as parked while the PR is being revised.
      [ -n "$TISS" ] && gh issue edit "$TISS" --repo "$SLUG" --add-label agent:working \
        --remove-label needs-human --remove-label agent:blocked >/dev/null 2>&1
      # The owner's answer is the statement "the world changed — try again": every budget on this
      # PR restarts and the park markers clear, so a future park can speak again. Both halves are
      # needed — the merge budgets, or a merge-parked PR re-parks on its very next merge attempt,
      # and the lane ledger, or an exhausted fix/review/selfreview lane never dispatches again.
      merge_attempt_clear "$TPR"; merge_stall_clear "$TPR"; merge_stall_cycle_clear "$TPR"
      rm -f "$BASE/state/mergepark-$TPR.sha"
      ledger_reset pr "$TPR"
      rm -f "$BASE/state/lanepark-$TPR-"* 2>/dev/null
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
# The human-hold filter is NOT optional here: this lane merges. Without it a maintainer who parks a
# green PR with `needs-human` — but leaves `agent:working` on, which is the natural thing to do —
# has it merged on the next tick. The phasefix lane already filters both labels; these two did not.
for MPR in $(gh pr list --repo "$SLUG" --state open --label "agent:working" \
               --json number,labels \
               --jq 'map(select((.labels|map(.name))
                      | (index("needs-human")|not) and (index("agent:blocked")|not)))
                     | sort_by(.number)|.[].number' 2>/dev/null); do
  pr_admissible "$MPR" "agent:working" || continue
  MBR="$(gh pr view "$MPR" --repo "$SLUG" --json headRefName --jq .headRefName 2>/dev/null)"
  [ -z "$MBR" ] && continue
  [ "$(gh pr view "$MPR" --repo "$SLUG" --json mergeStateStatus --jq .mergeStateStatus 2>/dev/null)" = "DIRTY" ] && continue
  MROLL="$(gh pr view "$MPR" --repo "$SLUG" --json statusCheckRollup 2>/dev/null \
    | jq -r "[.statusCheckRollup[]? | {n:(.name//.context//\"\"), s:(.conclusion//.state//\"PENDING\")}
             | $REQUIRED_CHECKS_JQ]")"
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
  # Last gate before any merge: closing an issue must not drop a declared later phase. A guard
  # park is a state change for THIS PR only — scan the next candidate instead of spending the
  # whole tick on it (head-of-line: one held PR must never starve the green PRs behind it).
  phase_guard_ok "$MPR" || { TICK_OUTCOME="skipped"; TICK_REASON="phase_guard_parked"; TICK_PR="$MPR"; continue; }
  # The merge lanes were the only PR-mutating lanes taking NO branch claim, so a concurrent slot
  # (or, later, the v2 daemon mid-park) could re-arm auto-merge under them. Claimed = skip.
  claim_branch "$MBR" || { log "fast path: PR #$MPR claimed by another slot — trying next."; continue; }
  log "MERGE-READY FAST PATH: PR #$MPR fully passes the gate — merging ahead of revise/fix work."
  TICK_OUTCOME="dispatched"; TICK_REASON="mode_merge_fastpath"; TICK_MODE="merge"; TICK_PR="$MPR"
  if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: would fast-path merge #$MPR."; exit 0; fi
  # rc=0: merged or genuinely queued — the queue owns it, this tick's work is done. rc=1: parked/
  # closed/not-taken — NO progress is possible on this PR, so keep scanning instead of the old
  # unconditional `exit 0` that burned 45 ticks in 6h re-polling #1120 while green PRs waited.
  merge_pr "$MPR" "✅ CI green, review satisfied & all threads resolved — merging (fast path)." && exit 0
  log "fast path: PR #$MPR made no merge progress — scanning the next candidate this same tick."
done

# ---- PHASEFIX LANE: the merge gate held a PR that closes a multi-phase issue with the remainder
# untracked (label agent:phasefix). Filing + linking the follow-up is mechanical, so an agent does
# it and hands the PR back to agent:working; the owner is only pulled in when phase_guard_ok gives
# up (MAX_PHASEFIX_ATTEMPTS). Runs before revise: a phasefix hold blocks an otherwise-green merge.
# A PR the agent escalated (or a human parked) keeps its labels, so filter the human holds out —
# otherwise this lane would re-dispatch on top of a deliberate `needs-human` every 5 minutes.
for FJSON in $(gh pr list --repo "$SLUG" --state open --label "agent:phasefix" \
  --json number,headRefName,labels \
  --jq 'map(select((.labels|map(.name)) | (index("needs-human")|not) and (index("agent:blocked")|not)))
        | sort_by(.number)|.[]|@base64' 2>/dev/null); do
  FPR="$(echo "$FJSON" | base64 -d | jq -r .number)"
  FBR="$(echo "$FJSON" | base64 -d | jq -r .headRefName)"
  pr_admissible "$FPR" "agent:phasefix" || continue
  # Claim FIRST: an in-flight phasefix run holds this branch, and it is about to relabel the PR.
  # Judging (or parking) it from another slot mid-run would race that hand-back.
  if ! claim_branch "$FBR"; then
    log "PR #$FPR (phasefix) claimed by another slot — trying next."
    continue
  fi
  FISS="$(closing_issue_for_pr "$FPR")"
  # Stale hold: the PR closes nothing any more — option (b), the closing keyword was dropped — so
  # the guard has nothing left to hold. Hand it back to the merge loop instead of burning a run.
  if [ -z "$FISS" ]; then
    log "PR #$FPR — phasefix hold is stale (the PR closes no issue) — returning it to the merge loop."
    [ "$DRY_RUN" = "1" ] || gh pr edit "$FPR" --repo "$SLUG" --add-label agent:working --remove-label agent:phasefix >/dev/null 2>&1
    continue
  fi
  # The budget is enforced HERE, not only in phase_guard_ok: a run that dies before handing the PR
  # back to agent:working never re-enters the guard, so without this the label would re-dispatch a
  # Claude run every tick forever.
  FCNT="$(phasefix_attempts "$FPR")"
  if [ "$FCNT" -ge "$MAX_PHASEFIX_ATTEMPTS" ]; then
    log "PR #$FPR — still held after $FCNT phasefix dispatch(es) — parking to human."
    TICK_OUTCOME="escalated"; TICK_REASON="phasefix_exhausted"; TICK_PR="$FPR"; TICK_BRANCH="$FBR"
    phase_park_to_human "$FPR" "$FISS" "$(phase_leftover "$FISS")" "$FCNT"
    exit 0
  fi
  log "PR #$FPR — phase-guard hold — dispatching MODE=phasefix to file/link the follow-up (issue #$FISS)."
  TICK_OUTCOME="dispatched"; TICK_REASON="mode_phasefix"; TICK_MODE="phasefix"; TICK_PR="$FPR"; TICK_BRANCH="$FBR"
  if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: would run MODE=phasefix for #$FPR ($FBR)."; exit 0; fi
  WT="$(add_worktree "$FBR" origin/main)"
  # Same hazard the START lane guards: an empty path would `cd ""` and run the agent in $HOME.
  if [ -z "$WT" ] || [ ! -d "$WT" ]; then
    log "PR #$FPR — worktree creation failed for $FBR; leaving the phasefix hold for the next tick."
    TICK_OUTCOME="failed"; TICK_REASON="worktree_create_failed"
    exit 1
  fi
  phasefix_bump "$FPR"
  export MODE=phasefix PR="$FPR" ISSUE="$FISS" BRANCH="$FBR" WORKTREE="$WT"
  run_claude "$WT" "Read $RUNBOOK and follow MODE=phasefix. PR=$FPR ISSUE=$FISS BRANCH=$FBR."
  exit 0
done

# Park a PR whose lane budget is spent. Same escalation contract as fix-exhausted: labels off the
# active set, draft (out of the merge queue's reach), owner assigned, issue mirrored, ONE Decision
# Comment. Un-park is the owner's answer through route_owner_answer, which resets the ledger.
# The comment is once-guarded per (pr, mode) so a re-scan can never re-post it.
park_lane_exhausted() {  # $1=pr $2=branch $3=mode $4=count $5=lane label to remove
  local P="$1" BR="$2" mode="$3" n="$4" lane_label="$5" ISS marker
  marker="$BASE/state/lanepark-$P-$mode"
  log "PR #$P — $mode budget spent ($n runs) — parking for the owner instead of burning another run."
  TICK_OUTCOME="escalated"; TICK_REASON="${mode}_exhausted"; TICK_PR="$P"; TICK_BRANCH="$BR"
  [ "$DRY_RUN" = "1" ] && { log "DRY_RUN: would park PR #$P (${mode}_exhausted)."; return 0; }
  gh pr edit "$P" --repo "$SLUG" --add-label needs-human --add-label agent:blocked \
    --remove-label "$lane_label" >/dev/null 2>&1
  gh pr ready --undo "$P" --repo "$SLUG" >/dev/null 2>&1
  gh pr edit "$P" --repo "$SLUG" --add-assignee "$ASSIGNEE" >/dev/null 2>&1
  # Mirror the FULL fix-exhausted contract onto the issue — needs-human alone leaves agent:working
  # on it, so the issue keeps reading as in-flight (and counting against the WIP gate) while its PR
  # is parked.
  ISS="$(issue_for_pr "$P")"
  [ -n "$ISS" ] && gh issue edit "$ISS" --repo "$SLUG" --add-label needs-human --add-label agent:blocked \
    --add-assignee "$ASSIGNEE" --remove-label agent:working >/dev/null 2>&1
  if [ ! -f "$marker" ]; then
    : > "$marker"
    # Options are numbered `1A/1B/1C`, NOT bare letters: answer_verdict() only accepts `ok` or a
    # <number><letter> token, so a reply of plain "B" is not an answer and the PR would stay parked
    # forever — with the budget that only route_owner_answer resets still spent.
    gh pr comment "$P" --repo "$SLUG" --body "## 🧑‍⚖️ Human decision needed — reply with option letters
Held (\`needs-human\`, risk: the \`$mode\` lane spent its budget of $n runs on this PR without getting it through). Each run either failed, timed out, or didn't move the gate, so re-running it would burn the same tokens for the same result — the PR is parked (draft, lane label off).
Reply one letter per question — e.g. \`1A\` — or \`ok\` for all recommendations. Your answer resets the budget.

### 1. How should we proceed?
- **A. Re-run the \`$mode\` lane** — I've addressed the blocker in the thread above; try again as-is.
- **B. I'll push the needed change myself** — pick the PR back up afterwards  ✅ *recommended*
- **C. Close this PR** — the approach is wrong, not just stuck.

**My recommendation: \`1B\`.** $n automated runs failing the same way is usually a blocker the lane can't see, so the cheapest unblock is a human push before the next run." >/dev/null 2>&1
  fi
}

# ---- REVISE LANE: the owner reviewed a PR and requested changes (label agent:revise) ----
# Claude implements the OWNER's feedback (not Copilot's), then hands the PR forward to merge.
# Iterates candidates so a claim held by another slot doesn't starve the rest of the lane.
for RJSON in $(gh pr list --repo "$SLUG" --state open --label "agent:revise" \
  --json number,headRefName --jq 'sort_by(.number)|.[]|@base64' 2>/dev/null); do
  RPR="$(echo "$RJSON" | base64 -d | jq -r .number)"
  RBR="$(echo "$RJSON" | base64 -d | jq -r .headRefName)"
  pr_admissible "$RPR" "agent:revise" || continue
  if ! claim_branch "$RBR"; then
    log "PR #$RPR (revise) claimed by another slot — trying next."
    continue
  fi
  # Previously unbounded: a revise run that failed or timed out was re-dispatched every tick,
  # forever — the same 45-minute burn on repeat. Budgeted now; the owner's answer resets it.
  RTRIES="$(ledger_count pr "$RPR" revise)"
  if [ "$RTRIES" -ge "$MAX_REVISE_ATTEMPTS" ]; then
    park_lane_exhausted "$RPR" "$RBR" revise "$RTRIES" "agent:revise"
    continue
  fi
  log "PR #$RPR — owner requested changes (agent:revise) — implementing their feedback (run $((RTRIES+1))/$MAX_REVISE_ATTEMPTS)."
  TICK_OUTCOME="dispatched"; TICK_REASON="mode_revise"; TICK_MODE="revise"; TICK_PR="$RPR"; TICK_BRANCH="$RBR"
  if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: would run MODE=revise for #$RPR ($RBR)."; exit 0; fi
  ledger_charge pr "$RPR" revise >/dev/null
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
    --json number,headRefName,mergeStateStatus,labels \
    --jq 'map(select((.labels|map(.name))
           | (index("needs-human")|not) and (index("agent:blocked")|not)))
          | sort_by((if .mergeStateStatus=="DIRTY" then 0 else 1 end), .number)|.[]|@base64' 2>/dev/null); do
  PR="$(echo "$PR_JSON" | base64 -d | jq -r .number)"
  pr_admissible "$PR" "agent:working" || continue
  WORKING_PRS=$((WORKING_PRS + 1))
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
        TICK_OUTCOME="dispatched"; TICK_REASON="mode_rebase_scripted"; TICK_MODE="rebase"; TICK_PR="$PR"; TICK_BRANCH="$BRANCH"
        exit 0
      fi
      log "PR #$PR — scripted rebase clean but push rejected — falling back to Claude MODE=rebase."
    else
      git -C "$WT" rebase --abort >/dev/null 2>&1
      log "PR #$PR — real conflicts — invoking Claude MODE=rebase."
    fi
    TICK_OUTCOME="dispatched"; TICK_REASON="mode_rebase"; TICK_MODE="rebase"; TICK_PR="$PR"; TICK_BRANCH="$BRANCH"
    export MODE=rebase PR ISSUE WORKTREE="$WT" BRANCH
    run_claude "$WT" "Read $RUNBOOK and follow MODE=rebase. PR=$PR ISSUE=$ISSUE BRANCH=$BRANCH."
    exit 0
  fi

  # Only the branch-protection REQUIRED checks gate merge — ignore non-required noise
  # (CodeQL Security Analysis, E2E, Docstring & Lint Gate). See REQUIRED_CHECKS_JQ at the top.
  ROLLUP="$(gh pr view "$PR" --repo "$SLUG" --json statusCheckRollup \
    | jq -r "[.statusCheckRollup[]? | {n:(.name//.context//\"\"), s:(.conclusion//.state//\"PENDING\")}
             | $REQUIRED_CHECKS_JQ]")"
  FAILED="$(echo "$ROLLUP" | jq '[.[]|select(.s=="FAILURE" or .s=="ERROR" or .s=="TIMED_OUT" or .s=="CANCELLED")]|length')"
  PENDING="$(echo "$ROLLUP" | jq '[.[]|select(.s=="PENDING" or .s=="QUEUED" or .s=="IN_PROGRESS" or .s=="EXPECTED")]|length')"
  UNRESOLVED="$(copilot_unresolved_threads "$PR")"
  CP_AT="$(copilot_last_review_at "$PR")"

  # The lane budgets count CONSECUTIVE failures, not lifetime runs: a stage that has PASSED is
  # proof the lane's last run worked, so its meter restarts. Without this, two SUCCESSFUL
  # selfreviews over a long PR's life would exhaust the budget and park a healthy PR.
  # (selfreview resets at the marker check in step 4 — the fresh marker is its proof of success.)
  # The fix meter resets on GREEN, never on merely "not failing": the tick right after a fix run
  # pushes sees every required check QUEUED, so FAILED is 0 while nothing has passed yet. Resetting
  # there refills the meter between every attempt — MAX_FIX_ATTEMPTS becomes unreachable and the
  # escalate-to-human contract silently disappears for any fix run that manages to push at all.
  [ "${FAILED:-0}" -eq 0 ] && [ "${PENDING:-0}" -eq 0 ] && ledger_reset pr "$PR" fix
  [ "${UNRESOLVED:-0}" -eq 0 ] && ledger_reset pr "$PR" review

  # 1) CI failing -> fix (or escalate after too many tries)
  if [ "${FAILED:-0}" -gt 0 ]; then
    if ! claim_branch "$BRANCH"; then log "PR #$PR claimed by another slot — trying next."; continue; fi
    # DISPATCHED RUNS, not `rev-list --count origin/main..` — commits are the wrong proxy in both
    # directions: a branch carrying 4 commits of legitimate feature work arrived pre-exhausted and
    # escalated before its FIRST fix run, while a fix run that died before committing was free and
    # retried forever. The ledger charges when the run is dispatched, so both count correctly.
    ATTEMPTS="$(ledger_count pr "$PR" fix)"
    if [ "${ATTEMPTS:-0}" -ge "$MAX_FIX_ATTEMPTS" ]; then
      log "PR #$PR failing after $ATTEMPTS fix runs — escalating to human."
      TICK_OUTCOME="escalated"; TICK_REASON="fix_exhausted"; TICK_PR="$PR"; TICK_BRANCH="$BRANCH"
      if [ "$DRY_RUN" != "1" ]; then
        gh pr edit "$PR" --repo "$SLUG" --add-label needs-human --add-label agent:blocked --remove-label agent:working >/dev/null 2>&1
        gh pr ready --undo "$PR" --repo "$SLUG" >/dev/null 2>&1
        [ -n "$ISSUE" ] && gh issue edit "$ISSUE" --repo "$SLUG" --add-label needs-human --add-label agent:blocked --add-assignee "$ASSIGNEE" --remove-label agent:working >/dev/null 2>&1
        if ! auto_fix_gave_up_p "$PR"; then
          gh pr comment "$PR" --repo "$SLUG" --body "🚧 Auto-fix gave up after $ATTEMPTS fix runs. Assigning @$ASSIGNEE — CI is still red." >/dev/null 2>&1
        fi
      fi
      exit 0
    fi
    ATTEMPTS=$((ATTEMPTS + 1))   # this run's number — exported for the RUNBOOK's own give-up rule
    log "PR #$PR CI failing (fix run $ATTEMPTS/$MAX_FIX_ATTEMPTS) — invoking fix."
    TICK_OUTCOME="dispatched"; TICK_REASON="mode_fix"; TICK_MODE="fix"; TICK_PR="$PR"; TICK_BRANCH="$BRANCH"
    if [ "$DRY_RUN" != "1" ]; then ledger_charge pr "$PR" fix >/dev/null; fi
    WT="$(add_worktree "$BRANCH" origin/main)"
    export MODE=fix PR ISSUE WORKTREE="$WT" BRANCH ATTEMPTS
    run_claude "$WT" "Read $RUNBOOK and follow MODE=fix. PR=$PR ISSUE=$ISSUE BRANCH=$BRANCH ATTEMPTS=$ATTEMPTS." "$(model_for_issue "$ISSUE")"
    exit 0
  fi

  # 2) Copilot has unresolved review threads -> address + resolve them BEFORE any merge
  if [ "${UNRESOLVED:-0}" -gt 0 ]; then
    if ! claim_branch "$BRANCH"; then log "PR #$PR claimed by another slot — trying next."; continue; fi
    # Previously unbounded — a review run that couldn't resolve the threads re-ran every tick.
    RVTRIES="$(ledger_count pr "$PR" review)"
    if [ "$RVTRIES" -ge "$MAX_REVIEW_ATTEMPTS" ]; then
      park_lane_exhausted "$PR" "$BRANCH" review "$RVTRIES" "agent:working"
      continue
    fi
    log "PR #$PR — $UNRESOLVED unresolved Copilot thread(s) — invoking review-address (run $((RVTRIES+1))/$MAX_REVIEW_ATTEMPTS)."
    TICK_OUTCOME="dispatched"; TICK_REASON="mode_review"; TICK_MODE="review"; TICK_PR="$PR"; TICK_BRANCH="$BRANCH"
    if [ "$DRY_RUN" != "1" ]; then ledger_charge pr "$PR" review >/dev/null; fi
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
  # A fresh adversarial marker is the selfreview lane's proof of success, so the meter restarts
  # HERE. It cannot restart inside the `REVIEW_OK != 1` branch below: best_review_at() includes the
  # marker and applies the same grace rule, so a fresh marker always implies REVIEW_OK=1 — a reset
  # down there is unreachable and the budget silently becomes lifetime-only, parking a healthy PR
  # on its third legitimate review.
  MARKER_FRESH=0
  claude_marker_fresh_p "$PR" "$HEAD_DATE" && { MARKER_FRESH=1; ledger_reset pr "$PR" selfreview; }
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
          # Same two rules as the other merge sites: a held/parked PR yields to the next
          # candidate rather than spending the tick, and the merge takes the branch claim.
          phase_guard_ok "$PR" || { TICK_OUTCOME="skipped"; TICK_REASON="phase_guard_parked"; TICK_PR="$PR"; continue; }
          claim_branch "$BRANCH" || { log "PR #$PR claimed by another slot — trying next."; continue; }
          log "PR #$PR — Copilot review overdue; REVIEW_FALLBACK=merge — merging with warning."
          merge_pr "$PR" "⚠️ Merging with CI green but WITHOUT a review — Copilot never delivered and REVIEW_FALLBACK=merge." && exit 0
          log "PR #$PR made no merge progress — scanning the next in-flight PR this same tick."
          continue ;;
      esac
      # REVIEW_FALLBACK=claude falls through to the adversarial review below.
    fi
    # Default reviewer: Claude adversarial review (fresh context; finds AND fixes; posts the
    # marker comment the gate accepts). One run per stale/missing review.
    if [ "$MARKER_FRESH" = "1" ]; then   # already reset above; reuses that answer, no second API call
      log "PR #$PR — green, fresh Claude adversarial marker already present."
    else
      if ! claim_branch "$BRANCH"; then log "PR #$PR claimed by another slot — trying next."; continue; fi
      # Previously unbounded — a selfreview that kept dying (or kept refusing to post its marker)
      # re-ran on every tick. Budget it; the escalation contract already covers the "cannot safely
      # fix" case (RUNBOOK), this covers the silent-failure case the contract can't see.
      SRTRIES="$(ledger_count pr "$PR" selfreview)"
      if [ "$SRTRIES" -ge "$MAX_SELFREVIEW_ATTEMPTS" ]; then
        park_lane_exhausted "$PR" "$BRANCH" selfreview "$SRTRIES" "agent:working"
        continue
      fi
      log "PR #$PR — green, no fresh review — invoking Claude adversarial review (run $((SRTRIES+1))/$MAX_SELFREVIEW_ATTEMPTS)."
      TICK_OUTCOME="dispatched"; TICK_REASON="mode_selfreview"; TICK_MODE="selfreview"; TICK_PR="$PR"; TICK_BRANCH="$BRANCH"
      if [ "$DRY_RUN" = "1" ]; then log "DRY_RUN: would run MODE=selfreview for #$PR."; exit 0; fi
      ledger_charge pr "$PR" selfreview >/dev/null
      WT="$(add_worktree "$BRANCH" origin/main)"
      export MODE=selfreview PR ISSUE WORKTREE="$WT" BRANCH
      run_claude "$WT" "Read $RUNBOOK and follow MODE=selfreview. PR=$PR ISSUE=$ISSUE BRANCH=$BRANCH MARKER='$CLAUDE_REVIEW_MARKER'." "$(model_for_issue "$ISSUE")"
      exit 0
    fi
  fi

  # 5) Green + fresh review (Copilot or Claude marker) + all threads resolved -> merge.
  # Same last gate as the fast path: never close an issue that still declares a declared later
  # phase — and same head-of-line rule: a held or unmergeable PR yields to the next one.
  phase_guard_ok "$PR" || { TICK_OUTCOME="skipped"; TICK_REASON="phase_guard_parked"; TICK_PR="$PR"; TICK_BRANCH="$BRANCH"; continue; }
  claim_branch "$BRANCH" || { log "PR #$PR claimed by another slot — trying next."; continue; }
  log "PR #$PR — merge gate satisfied. Requesting merge."
  TICK_OUTCOME="dispatched"; TICK_REASON="mode_merge"; TICK_MODE="merge"; TICK_PR="$PR"; TICK_BRANCH="$BRANCH"
  merge_pr "$PR" "✅ CI green, review satisfied & all threads resolved — merging." && exit 0
  log "PR #$PR made no merge progress — scanning the next in-flight PR this same tick."
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
  TICK_OUTCOME="skipped"; TICK_REASON="degraded_hold_new_start"; exit 0
fi
ISSUE="$(select_next_issue)"
if [ -z "$ISSUE" ]; then
  log "No agent:ready issues remaining. Pipeline idle."
  explain_empty_queue
  TICK_OUTCOME="nothing_to_do"; TICK_REASON="no_ready"; exit 0
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
TICK_OUTCOME="dispatched"; TICK_REASON="mode_start"; TICK_MODE="start"; TICK_ISSUE="$ISSUE"; TICK_BRANCH="$BRANCH"
if [ "$DRY_RUN" = "1" ]; then
  log "DRY_RUN: would create worktree $BRANCH and run MODE=start for #$ISSUE."
  exit 0
fi
gh issue edit "$ISSUE" --repo "$SLUG" --add-label agent:working --remove-label agent:ready >/dev/null 2>&1
WT="$(add_worktree "$BRANCH" origin/main)"
# add_worktree returning empty means it could not build the tree. Handing that to run_claude would
# `cd ""` — i.e. run the agent in $HOME, against the wrong repo — while the issue sits claimed as
# agent:working. Give the claim straight back instead; the reaper is the backstop, not the plan.
if [ -z "$WT" ] || [ ! -d "$WT" ]; then
  log "Issue #$ISSUE — worktree creation failed for $BRANCH; returning it to agent:ready."
  gh issue edit "$ISSUE" --repo "$SLUG" --add-label agent:ready --remove-label agent:working >/dev/null 2>&1
  TICK_OUTCOME="failed"; TICK_REASON="worktree_create_failed"
  exit 1
fi
export MODE=start ISSUE WORKTREE="$WT" BRANCH RISK
run_claude "$WT" "Read $RUNBOOK and follow MODE=start. ISSUE=$ISSUE BRANCH=$BRANCH RISK=$RISK WORKTREE=$WT." "$MODEL"
exit 0
