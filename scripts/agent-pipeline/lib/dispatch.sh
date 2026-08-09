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
OLLAMA_DEFAULT_TIER="${OLLAMA_DEFAULT_TIER:-lem-agent-tier2}"   # kimi-k2.7-code:cloud

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
# Issue labels can force a tier: agent:tier:1 | agent:tier:2 | agent:tier:2-alt | agent:tier:3.
# Otherwise MODE drives it: review/reasoning -> tier3 (nemotron), everything else -> default tier2.
# tier1 (glm-5.2:cloud) is opt-in only — it is a slow thinking model and intermittently times out,
# so it is reserved for issues the owner explicitly flags as hardest long-horizon work.
# agent:tier:2 is a real PIN, not a synonym for "no label": it holds tier2 against the MODE default
# (a labelled review would otherwise run tier3) and against a changed OLLAMA_DEFAULT_TIER. A label
# the owner can apply but that never changes the tier is the #1228 failure in a new costume.
_pick_ollama_tier() {
  local labels="${ISSUE_LABELS:-}"
  if [ -n "$labels" ]; then
    case " $labels " in
      *" agent:tier:1 "*)     echo "lem-agent-tier1"     ; return ;;
      *" agent:tier:3 "*)     echo "lem-agent-tier3"     ; return ;;
      *" agent:tier:2-alt "*) echo "lem-agent-tier2-alt" ; return ;;
      *" agent:tier:2 "*)     echo "lem-agent-tier2"     ; return ;;
    esac
  fi
  case "${MODE:-}" in
    selfreview|review) echo "lem-agent-tier3" ;;   # nemotron = reviewer/reasoning lane
    *)                 echo "$OLLAMA_DEFAULT_TIER" ;;
  esac
}

# ── context-window mapping for Ollama lane ──────────────────────────────────
# The `claude` CLI does not recognize LiteLLM aliases (lem-agent-tierN), so without
# a hint it auto-compacts against an assumed 200k window. The underlying Ollama Cloud
# models have larger real windows; this mapping lets us export
# CLAUDE_CODE_MAX_CONTEXT_TOKENS per tier so auto-compact manages the real limit.
# Values are sourced from the models' public docs / model cards as of 2026-08-09.
# Each is overridable via config.env so a deployment-specific cap can be honoured.
#   glm-5.2         -> 1,048,576 (1M)   — Zhipu/GLM docs, NVIDIA NIM, Unsloth
#   kimi-k2.7-code  ->   262,144 (256K) — Moonshot AI Kimi docs
#   minimax-m3      ->   524,288 (512K) — MiniMax guarantees at least 512K (up to 1M)
#   nemotron-3-super ->  262,144 (256K) — model card says up to 1M; default deployments 256K
_ollama_tier_context_tokens() {
  case "${1:-}" in
    lem-agent-tier1)     echo "${TIER1_CONTEXT_TOKENS:-1048576}" ;;
    lem-agent-tier2)     echo "${TIER2_CONTEXT_TOKENS:-262144}" ;;
    lem-agent-tier2-alt) echo "${TIER2_ALT_CONTEXT_TOKENS:-524288}" ;;
    lem-agent-tier3)     echo "${TIER3_CONTEXT_TOKENS:-262144}" ;;
    *)                   echo "${DEFAULT_CONTEXT_TOKENS:-262144}" ;;
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

  # Tell the Claude CLI the real context window for the LiteLLM alias it does not
  # recognize, so auto-compact works against the model's limit instead of 200k.
  if [ "$LANE" = "ollama" ]; then
    CLAUDE_CODE_MAX_CONTEXT_TOKENS="$(_ollama_tier_context_tokens "$AGENT_TIER")"
    export CLAUDE_CODE_MAX_CONTEXT_TOKENS
  fi

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