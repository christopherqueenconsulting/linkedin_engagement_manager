#!/usr/bin/env bash
# Lane executor — the single place a `claude -p` run is launched, wrapped with capacity-aware lane
# env, PostHog lifecycle telemetry, outcome recording, and GitHub labeling. run_claude() in tick.sh
# delegates here so every MODE (depfix/revise/rebase/fix/review/selfreview/start) gets identical
# routing + observability without per-call-site edits. Runs headless with --dangerously-skip-permissions
# (the same flag tick.sh's original run_claude uses) so the unattended cron pipeline can execute.
#
# Env read (set by tick.sh before each call): MODE, ISSUE, PR, BRANCH, WORKTREE, RISK, SLOT,
#   WORKER_ID, EXECUTION_ID, _TICK_LOG, LOG, LOGDIR, CLAUDE_TIMEOUT, DRY_RUN.
# Args: $1=worktree  $2=prompt  $3=claude_model_hint (sonnet|haiku|opus|"" — used only on the claude lane)
#
# Telemetry contract:
#   - This wrapper owns the LIFECYCLE view (issue_assigned, ai_call_started/completed/failed,
#     issue_completed/failed, fallback_triggered, lane_escalation_triggered) with latency + success.
#   - Per-call TOKEN/COST for the Ollama lane is owned by LiteLLM's native $ai_generation PostHog
#     callback (already configured in .litellm/config.yaml) — never duplicated here. The Claude
#     lane is a flat-rate subscription, so it carries no per-call cost. tokens_* are 0 here by
#     design; join on execution_id + model for the full picture.
BASE="${BASE:-/home/lem/agent-pipeline}"
# shellcheck disable=SC1091
. "$BASE/lib/dispatch.sh"
# shellcheck disable=SC1091
. "$BASE/lib/labels.sh" 2>/dev/null || true
[ -f "$BASE/secrets.env" ] && . "$BASE/secrets.env" 2>/dev/null
# Sourcing makes these shell variables, not environment variables — a `claude -p` child (and any
# script a tick runs inside the worktree) would never see them. LEM issue #842 decision `1B`: the
# model benchmark is meant to run UNATTENDED from this runner's env, so the four vars it reads are
# exported here. Everything else in secrets.env stays shell-local on purpose.
export OLLAMA_CLOUD_URL="${OLLAMA_CLOUD_URL:-}" OLLAMA_CLOUD_API_KEY="${OLLAMA_CLOUD_API_KEY:-}"
export BENCHMARK_ENABLED="${BENCHMARK_ENABLED:-}" \
       BENCHMARK_USAGE_LEVELS="${BENCHMARK_USAGE_LEVELS:-}"

MCP_CONFIG="$BASE/mcp/mcp-config.json"
OLLAMA_LITELLM_URL="${OLLAMA_LITELLM_URL:-http://127.0.0.1:4000}"

# Emit one lifecycle/AI event with the common context. <event> <extra-json-via-_EMIT_EXTRA>
_emit() {
  posthog_capture "$1" "agent-pipeline" "$(python3 -c '
import json,os
base={"lem_component":"agent-pipeline","environment":os.environ.get("ENVIRONMENT","production"),
 "repo":os.environ.get("REPO","christopherqueenconsulting/linkedin_engagement_manager"),
 "execution_id":os.environ.get("EXECUTION_ID",""),"worker_id":os.environ.get("WORKER_ID",""),
 "lane":os.environ.get("LANE",""),"provider":("ollama-cloud" if os.environ.get("LANE")=="ollama" else "claude-subscription"),
 "model":os.environ.get("AGENT_MODEL",""),"model_tier":os.environ.get("AGENT_TIER",""),
 "route_reason":os.environ.get("ROUTE_REASON",""),"issue_number":os.environ.get("ISSUE",""),
 "pr_number":os.environ.get("PR",""),"issue_priority":os.environ.get("ISSUE_PRIORITY",""),
 "issue_type":os.environ.get("MODE","")}
extra={}
try: extra=json.loads(os.environ.get("_EMIT_EXTRA","{}") or "{}")
except Exception: extra={}
print(json.dumps({**base,**extra}))
' 2>/dev/null)" || true
}

run_lane() {  # $1=worktree  $2=prompt  $3=claude_model_hint
  local wt="$1" prompt="$2" hint="${3:-}" out rc t0 ms

  # EVERY lane runs its agent inside its OWN git worktree, and this is the one place that is
  # enforced. Below, the agent is launched as `( cd "$wt" && claude ... )` — and `cd ""` in bash
  # SUCCEEDS as a no-op, so an empty $wt does not fail: it silently runs the agent in whatever
  # directory the tick happens to be in. That is the shared checkout, where concurrent slots would
  # then edit the same files and clobber each other.
  #
  # `add_worktree` returns empty on failure, and its callers capture stdout without checking the
  # exit code. Two of the nine lanes (start, phasefix) guard it at the call site; the other seven
  # did not. Guarding HERE covers all of them, and every lane added later, instead of relying on
  # nine copies of the same check staying in sync — which is exactly the failure mode this repo's
  # restructure kept finding in its own test guards.
  if [ -z "$wt" ] || [ ! -d "$wt" ]; then
    log "run_lane: REFUSING to dispatch — worktree path is empty or missing ('${wt}'). An agent must
never run in the shared checkout. MODE=${MODE:-?} BRANCH=${BRANCH:-?} ISSUE=${ISSUE:-?} PR=${PR:-?}"
    return 1
  fi
  # A directory alone is not proof: a worktree carries a `.git` FILE pointing at the parent repo
  # (a normal clone has a `.git` DIRECTORY). Refusing here catches a stale path that happens to
  # exist as a plain directory.
  if [ ! -e "$wt/.git" ]; then
    log "run_lane: REFUSING to dispatch — '$wt' is not a git worktree (no .git). MODE=${MODE:-?}"
    return 1
  fi

  dispatch_lane "$hint"

  # tick.sh snapshots TICK_LANE/TICK_MODEL/TICK_ROUTE_REASON before dispatch_lane() runs (it runs
  # inside this wrapper), so backfill them here so the EXIT trap's emit_tick_outcome() writes the
  # real routing dimensions into tick-outcomes.ndjson.
  TICK_LANE="${LANE:-}"
  TICK_MODEL="${AGENT_TIER:-${AGENT_MODEL:-}}"
  # A Claude run with no agent:model:* hint uses the CLI default — that is a REAL model choice, not
  # an absent one. Name it the way dispatch.sh's routing_decision_made event already does, so an
  # empty `model` in the file means "no lane ran on this tick", never "the default model ran".
  [ -z "$TICK_MODEL" ] && [ "${LANE:-}" = "claude" ] && TICK_MODEL="default"
  TICK_ROUTE_REASON="${ROUTE_REASON:-}"
  export TICK_LANE TICK_MODEL TICK_ROUTE_REASON

  if [ "${DRY_RUN:-0}" = "1" ]; then
    log "DRY_RUN: would run lane=$LANE model=${AGENT_MODEL:-default} tier=${AGENT_TIER:-} in $wt"
    return 0
  fi

  _emit "issue_assigned"
  _emit "ai_call_started"
  [ "$ROUTE_REASON" = "fallback" ] && _emit "fallback_triggered"
  { [ "$ROUTE_REASON" = "degraded" ] || [ "$ROUTE_REASON" = "escalated" ]; } && _emit "lane_escalation_triggered"

  [ -n "${AGENT_TIER:-}" ] && log "using lane=$LANE model=${AGENT_TIER} (reason=$ROUTE_REASON)"
  [ -z "${AGENT_TIER:-}" ] && [ -n "${AGENT_MODEL:-}" ] && log "using lane=$LANE model=$AGENT_MODEL (reason=$ROUTE_REASON)"

  out="$(mktemp "${LOGDIR:-/tmp}/run.XXXXXX")"
  t0="$(date +%s)"

  # claude lane = owner's Max login (no env override). ollama lane = same CLI pointed at LiteLLM.
  local model_arg=() mcp_arg=()
  if [ "$LANE" = "ollama" ]; then
    model_arg=(--model "${AGENT_TIER}")
  else
    [ -n "${AGENT_MODEL:-}" ] && model_arg=(--model "$AGENT_MODEL")
  fi
  [ -f "$MCP_CONFIG" ] && mcp_arg=(--mcp-config "$MCP_CONFIG")

  # Grant the agent the RUNBOOK's directory, NOT $BASE. $BASE holds config.env, secrets.env and
  # (before this change) the App private key, while the prompt an agent follows is assembled from
  # issue text written by strangers — the RUNBOOK's own prompt-injection section says exactly that.
  #
  # Be precise about what this buys: `--add-dir` scopes the FILE tools, but
  # `--dangerously-skip-permissions` leaves the Bash tool able to read anything this uid can read,
  # and agents run as the same uid as this runner. So this is defense in depth, NOT the control.
  # The control is custody: the App key is root-owned in /etc/lem and minted into a ~1h token by
  # lem-gh-token.timer, so the worst an agent can reach is a credential that expires within the
  # hour and carries authority the pipeline already has — instead of a key that never expires.
  local runbook_dir; runbook_dir="$(dirname "${RUNBOOK:-$BASE/RUNBOOK.md}")"
  if [ "$LANE" = "ollama" ]; then
    ( cd "$wt" && \
      ANTHROPIC_BASE_URL="$OLLAMA_LITELLM_URL" \
      ANTHROPIC_AUTH_TOKEN="$LITELLM_MASTER_KEY" \
      ANTHROPIC_API_KEY="$LITELLM_MASTER_KEY" \
      timeout "${CLAUDE_TIMEOUT:-45m}" claude -p "$prompt" \
        --dangerously-skip-permissions --add-dir "$runbook_dir" "${model_arg[@]}" "${mcp_arg[@]}" ) >"$out" 2>&1
    rc=$?
  else
    ( cd "$wt" && \
      unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY 2>/dev/null || true
      timeout "${CLAUDE_TIMEOUT:-45m}" claude -p "$prompt" \
        --dangerously-skip-permissions --add-dir "$runbook_dir" "${model_arg[@]}" "${mcp_arg[@]}" ) >"$out" 2>&1
    rc=$?
  fi
  ms=$(( ($(date +%s) - t0) * 1000 ))
  cat "$out" >> "${LOG:-/dev/null}"

  # The lane alone does not answer #1229's motivating question ("do Ollama-lane runs fail more often
  # than Claude-lane runs?"): a tick that dispatched an agent records tick_outcome="dispatched"
  # whether the agent exited 0 or 45 minutes into a timeout, and every `failed` row in the file is a
  # PRE-dispatch failure (worktree_create_failed, merge_not_taken) that carries no lane at all. So
  # record the agent's exit status as its own field. Additive on purpose: flipping tick_outcome to
  # "failed" here would break status.sh's stall detector (a failed agent run IS the pipeline doing
  # something) and inflate its per-PR failure counter. -1 = no agent ran on this tick.
  TICK_AGENT_RC="$rc"; export TICK_AGENT_RC

  # Usage-limit detection — only on a FAILED run. Lane-specific: a Claude usage-limit pauses ONLY
  # the Claude lane (Ollama keeps working the backlog); an Ollama usage-limit pauses only Ollama.
  # The probe loop in capacity.sh re-pauses hourly and resumes the lane the moment a probe succeeds.
  local ul=0
  if [ $rc -ne 0 ] && grep -qiE "$UL_REGEX" "$out" 2>/dev/null; then
    ul=1
    local pause_file="$CLAUDE_PAUSED_FILE"
    [ "$LANE" = "ollama" ] && pause_file="$OLLAMA_PAUSED_FILE"
    echo "$(( $(date +%s) + ${USAGE_PAUSE_MINUTES:-60} * 60 ))" > "$pause_file"
    log "usage/rate limit on a failed $LANE run — pausing $LANE lane for ${USAGE_PAUSE_MINUTES:-60}m."
  fi
  record_lane_outcome "$LANE" $([ $rc -eq 0 ] && echo 1 || echo 0) "$ul"

  _EMIT_EXTRA="{\"success\":$([ $rc -eq 0 ] && echo true || echo false),\"latency_ms\":$ms,\"retry_count\":0,\"tokens_in\":0,\"tokens_out\":0,\"total_tokens\":0,\"estimated_cost\":0,\"error_type\":\"$([ $rc -ne 0 ] && echo nonzero_exit)\",\"fallback_from\":\"${FALLBACK_FROM}\",\"fallback_to\":\"${FALLBACK_TO}\"}" \
    _emit "$([ $rc -eq 0 ] && echo ai_call_completed || echo ai_call_failed)"

  local kind="pr" num="${ISSUE:-${PR:-}}"
  [ -n "${ISSUE:-}" ] && kind="issue"
  [ -n "$num" ] && apply_lane_labels "$kind" "$num" "$LANE" "${AGENT_TIER:-${AGENT_MODEL:-}}" "$ROUTE_REASON"

  # Best-effort PR telemetry: for a fresh START the agent opens a PR during the run; for the other
  # MODEs the PR already existed and was updated. Detected once here so every MODE gets it.
  if [ $rc -eq 0 ]; then
    if [ "${MODE:-}" = "start" ] && [ -n "${ISSUE:-}" ]; then
      local _pr; _pr="$(pr_for_issue "$ISSUE" 2>/dev/null)"
      if [ -n "$_pr" ]; then
        PR="$_pr"; export PR
        _EMIT_EXTRA="{\"pr_number\":$_pr}" _emit "pr_opened"
        apply_lane_labels pr "$_pr" "$LANE" "${AGENT_TIER:-${AGENT_MODEL:-}}" "$ROUTE_REASON"
      fi
    elif [ -n "${PR:-}" ]; then
      _EMIT_EXTRA="{\"pr_number\":$PR}" _emit "pr_updated"
    fi
  fi

  if [ $rc -eq 0 ]; then
    _emit "issue_completed"
  else
    _EMIT_EXTRA="{\"error_type\":\"nonzero_exit\",\"error_message\":\"claude rc=$rc ($LANE)\"}" _emit "issue_failed"
    log "$LANE claude exited rc=$rc (timeout/interrupt/limit) — will retry next tick."
  fi
  rm -f "$out"
  return $rc
}