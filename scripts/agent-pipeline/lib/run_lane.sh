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
  if [ "${DRY_RUN:-0}" = "1" ]; then
    dispatch_lane "$hint"
    log "DRY_RUN: would run lane=$LANE model=${AGENT_MODEL:-default} tier=${AGENT_TIER:-} in $wt"
    return 0
  fi

  dispatch_lane "$hint"
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

  if [ "$LANE" = "ollama" ]; then
    ( cd "$wt" && \
      ANTHROPIC_BASE_URL="$OLLAMA_LITELLM_URL" \
      ANTHROPIC_AUTH_TOKEN="$LITELLM_MASTER_KEY" \
      ANTHROPIC_API_KEY="$LITELLM_MASTER_KEY" \
      timeout "${CLAUDE_TIMEOUT:-45m}" claude -p "$prompt" \
        --dangerously-skip-permissions --add-dir "$BASE" "${model_arg[@]}" "${mcp_arg[@]}" ) >"$out" 2>&1
    rc=$?
  else
    ( cd "$wt" && \
      unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY 2>/dev/null || true
      timeout "${CLAUDE_TIMEOUT:-45m}" claude -p "$prompt" \
        --dangerously-skip-permissions --add-dir "$BASE" "${model_arg[@]}" "${mcp_arg[@]}" ) >"$out" 2>&1
    rc=$?
  fi
  ms=$(( ($(date +%s) - t0) * 1000 ))
  cat "$out" >> "${LOG:-/dev/null}"

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