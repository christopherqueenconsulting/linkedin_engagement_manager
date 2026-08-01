#!/usr/bin/env bash
# Capacity-aware dispatcher for the agent pipeline.
#
# Decides which LANE handles a run (Claude subscription vs Ollama cloud via LiteLLM) and which
# MODEL/TIER to use, applying the >50% rule from the task:
#
#   Claude >50% AND Ollama >50%  -> Claude PRIMARY (slot 1) + Ollama PARALLEL (slot >=2)
#   Claude <=50%, Ollama >50%   -> Ollama (fallback)
#   Claude exhausted/away       -> Ollama (fallback, fallback_from=claude)
#   Claude >50%, Ollama <=50%    -> Claude only
#   both constrained            -> degraded (pick the higher-pct lane, low concurrency)
#
# Sources capacity.sh + posthog.sh + labels.sh. Two entry points:
#   capacity_preflight   — call ONCE near the top of tick.sh (sets CLAUDE_AVAIL/OLLAMA_AVAIL/DEGRADED
#                           so the CAP / degraded-mode logic can react before any run).
#   dispatch_lane         — call inside run_claude; sets LANE/AGENT_MODEL/AGENT_TIER/ROUTE_REASON
#                           (+FALLBACK_FROM/FALLBACK_TO) and emits a routing_decision_made event.
BASE="${BASE:-/home/lem/agent-pipeline}"
# shellcheck disable=SC1091
. "$BASE/lib/capacity.sh"
# shellcheck disable=SC1091
. "$BASE/lib/posthog.sh" 2>/dev/null || true

OLLAMA_PARALLEL_ENABLED="${OLLAMA_PARALLEL_ENABLED:-1}"
OLLAMA_DEFAULT_TIER="${OLLAMA_DEFAULT_TIER:-lem-agent-tier2}"   # kimi-k2.7-code

# ── preflight (once per tick) ───────────────────────────────────────────────
capacity_preflight() {
  local c o cp ct op ot
  read -r cp ct <<<"$(claude_capacity)"
  read -r op ot <<<"$(ollama_capacity)"
  CLAUDE_PCT="$cp"; CLAUDE_STATUS="$ct"
  OLLAMA_PCT="$op"; OLLAMA_STATUS="$ot"
  lane_available "$cp"; CLAUDE_AVAIL=$?     # 0 = available, 1 = not (bash truth)
  lane_available "$op"; OLLAMA_AVAIL=$?
  # Auto-recovery: if Claude is in a post-limit state (not hard-paused), probe once per interval to
  # decide whether usage has reset BEFORE we'd dispatch a real issue to it. May flip CLAUDE_*.
  _maybe_probe_claude 2>/dev/null || true
  # Same recovery contract for the Ollama lane (so an Ollama-Cloud limit self-heals too).
  _maybe_probe_ollama 2>/dev/null || true
  # Degraded = neither lane clearly above threshold. tick.sh forces CAP=1 and skips new starts.
  if [ "$CLAUDE_AVAIL" -ne 0 ] && [ "$OLLAMA_AVAIL" -ne 0 ]; then
    DEGRADED=1
  else
    DEGRADED=0
  fi
  export CLAUDE_PCT CLAUDE_STATUS OLLAMA_PCT OLLAMA_STATUS CLAUDE_AVAIL OLLAMA_AVAIL DEGRADED
  # One heartbeat event per tick so a quiet dashboard still shows capacity state.
  posthog_capture "capacity_preflight" "agent-pipeline" \
    "{\"claude_pct\":$cp,\"claude_status\":\"$ct\",\"ollama_pct\":$op,\"ollama_status\":\"$ot\",\"degraded\":$DEGRADED}"
  echo "[dispatch] claude=$cp%($ct) ollama=$op%($ot) degraded=$DEGRADED" >> "${_TICK_LOG:-/dev/null}" 2>/dev/null || true
}

# ── tier selection for the Ollama lane ──────────────────────────────────────
# Issue labels can force a tier: agent:tier:1 | agent:tier:2-alt | agent:tier:3.
# Otherwise MODE drives it: review/reasoning -> tier3 (nemotron), everything else -> default tier2.
# tier1 (glm-5.2) is opt-in only — it is a slow thinking model and intermittently times out,
# so it is reserved for issues the owner explicitly flags as hardest long-horizon work.
_pick_ollama_tier() {
  local labels="${ISSUE_LABELS:-}"
  if [ -n "$labels" ]; then
    case " $labels " in
      *" agent:tier:1 "*)     echo "lem-agent-tier1"     ; return ;;
      *" agent:tier:3 "*)     echo "lem-agent-tier3"     ; return ;;
      *" agent:tier:2-alt "*) echo "lem-agent-tier2-alt" ; return ;;
    esac
  fi
  case "${MODE:-}" in
    selfreview|review) echo "lem-agent-tier3" ;;   # nemotron = reviewer/reasoning lane
    *)                 echo "$OLLAMA_DEFAULT_TIER" ;;
  esac
}

# ── per-run dispatch (inside run_claude) ─────────────────────────────────────
# $1 = claude_model_hint (sonnet|haiku|opus|"" from model_for_issue). Exports LANE etc.
dispatch_lane() {
  local claude_hint="${1:-}"
  LANE=""; AGENT_MODEL=""; AGENT_TIER=""; ROUTE_REASON=""; FALLBACK_FROM=""; FALLBACK_TO=""

  # Determine issue/PR context for telemetry + tier labels (env set by tick.sh before the call).
  local num="${ISSUE:-${PR:-}}"
  local kind="issue"; [ -n "${PR:-}" ] && kind="pr"
  local issue_url=""; [ -n "${ISSUE:-}" ] && issue_url="https://github.com/${SLUG:-christopherqueenconsulting/linkedin_engagement_manager}/issues/$ISSUE"

  if [ "$CLAUDE_AVAIL" -eq 0 ] && [ "$OLLAMA_AVAIL" -eq 0 ]; then
    # Both healthy: slot 1 = Claude primary (highest-priority issue), slot >=2 = Ollama parallel.
    if [ "${SLOT:-1}" -ge 2 ] && [ "$OLLAMA_PARALLEL_ENABLED" = "1" ]; then
      LANE="ollama"; AGENT_TIER="$(_pick_ollama_tier)"; AGENT_MODEL="$AGENT_TIER"; ROUTE_REASON="parallel"
    else
      LANE="claude"; AGENT_MODEL="$claude_hint"; ROUTE_REASON="primary"
    fi
  elif [ "$OLLAMA_AVAIL" -eq 0 ]; then
    # Claude constrained/exhausted, Ollama healthy -> Ollama fallback.
    LANE="ollama"; AGENT_TIER="$(_pick_ollama_tier)"; AGENT_MODEL="$AGENT_TIER"; ROUTE_REASON="fallback"
    FALLBACK_FROM="claude"; FALLBACK_TO="ollama"
  elif [ "$CLAUDE_AVAIL" -eq 0 ]; then
    # Claude healthy, Ollama constrained -> Claude only.
    LANE="claude"; AGENT_MODEL="$claude_hint"; ROUTE_REASON="primary"
  else
    # Both constrained: degraded. Pick the higher-pct lane; flag low concurrency. Ties go to
    # Ollama (its failures are free — they don't burn the Max subscription like a Claude attempt does).
    LANE="degraded"; ROUTE_REASON="degraded"
    if [ "${CLAUDE_PCT:-0}" -gt "${OLLAMA_PCT:-0}" ]; then
      LANE="claude"; AGENT_MODEL="$claude_hint"
    else
      LANE="ollama"; AGENT_TIER="$(_pick_ollama_tier)"; AGENT_MODEL="$AGENT_TIER"
    fi
  fi

  export LANE AGENT_MODEL AGENT_TIER ROUTE_REASON FALLBACK_FROM FALLBACK_TO

  local provider="$LANE"
  [ "$LANE" = "ollama" ] && provider="ollama-cloud"
  local model_tier="${AGENT_TIER:-}"
  [ "$LANE" = "claude" ] && model_tier="${AGENT_MODEL:-default}"
  posthog_capture "routing_decision_made" "agent-pipeline" "$(python3 -c '
import json,os
print(json.dumps({
  "lane": os.environ.get("LANE",""),
  "provider": os.environ.get("provider",""),
  "model": os.environ.get("AGENT_MODEL",""),
  "model_tier": os.environ.get("model_tier",""),
  "route_reason": os.environ.get("ROUTE_REASON",""),
  "fallback_from": os.environ.get("FALLBACK_FROM",""),
  "fallback_to": os.environ.get("FALLBACK_TO",""),
  "issue_number": os.environ.get("ISSUE",""),
  "pr_number": os.environ.get("PR",""),
  "issue_url": os.environ.get("issue_url",""),
  "worker_id": os.environ.get("WORKER_ID",""),
  "issue_priority": os.environ.get("ISSUE_PRIORITY",""),
  "issue_type": os.environ.get("MODE",""),
}) if os.environ.get("ISSUE") or os.environ.get("PR") else {})
' 2>/dev/null)" || true
  echo "[dispatch] lane=$LANE model=${AGENT_MODEL:-default} tier=${AGENT_TIER:-} reason=$ROUTE_REASON" >> "${_TICK_LOG:-/dev/null}" 2>/dev/null || true
}