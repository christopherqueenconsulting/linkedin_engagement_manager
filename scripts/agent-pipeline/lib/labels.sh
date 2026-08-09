#!/usr/bin/env bash
# GitHub issue/PR labeling for the agent pipeline — encodes which LANE + MODEL + TIER handled a
# thread, in a human-readable and machine-filterable `ai:*` scheme. Idempotent: missing labels are
# created on first use; applying is add-only (we never remove another lane's label here — the tick
# state machine owns agent:working/agent:ready transitions).
#
# Scheme:
#   ai:claude-subscription            ai:ollama-cloud
#   ai:claude-subscription:<model>    ai:ollama-cloud:<tier>
#                                    ai:ollama-cloud:<modelslug>
#   ai:routed:parallel | ai:routed:fallback | ai:routed:escalated | ai:routed:degraded
# Model slugs: glm-5.2, kimi-k2.7-code, minimax-m3, nemotron-3-super; claude: sonnet|haiku|opus.
#
# Owner-applied tier pins are also bootstrapped here because a missing label makes
# `gh issue edit --add-label agent:tier:N` fail the whole edit silently (#1228).
#   agent:tier:1 | agent:tier:2 | agent:tier:2-alt | agent:tier:3
BASE="${BASE:-/home/lem/agent-pipeline}"
SLUG="${SLUG:-christopherqueenconsulting/linkedin_engagement_manager}"

# Anthropic lane model → label slug (the claude CLI --model hint we passed).
_claude_model_slug() {
  case "${1:-}" in
    sonnet|haiku|opus) echo "$1" ;;
    *)                 echo "" ;;
  esac
}

# Ollama tier alias (lem-agent-tierN[-alt]) → the cloud model slug used in labels.
# Mirrors the .litellm/config.yaml lem-agent-* mapping.
_ollama_model_slug() {
  case "${1:-}" in
    lem-agent-tier1)     echo "glm-5.2" ;;
    lem-agent-tier2)     echo "kimi-k2.7-code" ;;
    lem-agent-tier2-alt) echo "minimax-m3" ;;
    lem-agent-tier3)     echo "nemotron-3-super" ;;
    *)                   echo "" ;;
  esac
}

_ollama_tier_label() {
  case "${1:-}" in
    lem-agent-tier1)     echo "ai:ollama-cloud:tier1" ;;
    lem-agent-tier2)     echo "ai:ollama-cloud:tier2" ;;
    lem-agent-tier2-alt) echo "ai:ollama-cloud:tier2-alt" ;;
    lem-agent-tier3)     echo "ai:ollama-cloud:tier3" ;;
    *)                   echo "" ;;
  esac
}

# The full label set we promise exists. Created once by ensure_ai_labels.
AI_LABELS=(
  "ai:claude-subscription"
  "ai:claude-subscription:sonnet"
  "ai:claude-subscription:haiku"
  "ai:claude-subscription:opus"
  "ai:ollama-cloud"
  "ai:ollama-cloud:tier1"
  "ai:ollama-cloud:tier2"
  "ai:ollama-cloud:tier2-alt"
  "ai:ollama-cloud:tier3"
  "ai:ollama-cloud:glm-5.2"
  "ai:ollama-cloud:kimi-k2.7-code"
  "ai:ollama-cloud:minimax-m3"
  "ai:ollama-cloud:nemotron-3-super"
  "ai:routed:parallel"
  "ai:routed:fallback"
  "ai:routed:escalated"
  "ai:routed:degraded"
  # Owner-applied Ollama tier pins (#1228). These must exist before `gh issue edit` can
  # apply them, otherwise the edit fails silently and the override never routes.
  "agent:tier:1"
  "agent:tier:2"
  "agent:tier:2-alt"
  "agent:tier:3"
)

# Create any missing ai:* labels in the repo. Safe to run every tick (skips existing). One gh call
# per missing label — the set is tiny and fixed, so this is cheap and only writes the first time.
ensure_ai_labels() {
  local existing missing
  existing="$(gh label list --repo "$SLUG" --limit 200 --json name --jq '.[].name' 2>/dev/null || true)"
  # Match WHOLE lines, not substrings: `agent:tier:2` is a prefix of `agent:tier:2-alt` (same for
  # ai:ollama-cloud:tier2), so a substring test reports the shorter name as already present the
  # moment the longer one exists — and it is then never created. That is exactly the silent
  # missing-label trap #1228 is about, so the guard must not reintroduce it.
  existing=$'\n'"$existing"$'\n'
  for l in "${AI_LABELS[@]}"; do
    case "$existing" in *$'\n'"$l"$'\n'*) continue ;; esac
    # Purple family (#7c5cff) for lane/tier labels, slate (#6b7280) for routed:* reasons.
    local color="7c5cff"
    case "$l" in ai:routed:*) color="6b7280" ;; esac
    gh label create --repo "$SLUG" "$l" --color "$color" \
      --description "AI lane/model tag (agent pipeline)" >/dev/null 2>&1 || true
  done
}

# lane_labels_for <lane> <agent_model_or_alias> <route_reason>
# Echoes the space-separated label set to ADD for this run (lane + model + route reason).
lane_labels_for() {
  local lane="$1" model="$2" reason="${3:-}"
  local out=""
  case "$lane" in
    claude)
      out="ai:claude-subscription"
      local cs; cs="$(_claude_model_slug "$model")"
      [ -n "$cs" ] && out="$out ai:claude-subscription:$cs" ;;
    ollama)
      out="ai:ollama-cloud"
      local ts ms
      ts="$(_ollama_tier_label "$model")"; ms="$(_ollama_model_slug "$model")"
      [ -n "$ts" ] && out="$out $ts"
      [ -n "$ms" ] && out="$out ai:ollama-cloud:$ms" ;;
  esac
  case "$reason" in
    parallel)   out="$out ai:routed:parallel" ;;
    fallback)   out="$out ai:routed:fallback" ;;
    escalated)  out="$out ai:routed:escalated" ;;
    degraded)   out="$out ai:routed:degraded" ;;
  esac
  echo "$out"
}

# apply_lane_labels <pr|issue> <number> <lane> <model_or_alias> <route_reason>
# Adds the lane/model/route labels to the thread. Errors are swallowed (labeling is observability,
# not control flow) — the state machine's own labels are what gate work.
apply_lane_labels() {
  local kind="$1" num="$2" lane="$3" model="$4" reason="${5:-}"
  local labels; labels="$(lane_labels_for "$lane" "$model" "$reason")"
  [ -n "$labels" ] || return 0
  # gh's --add-label splits on COMMAS (or repeated flags), not spaces — a space-separated
  # string is parsed as one bogus label that doesn't exist and silently no-ops. lane_labels_for
  # joins with spaces, so collapse to commas here. Label names contain colons but no spaces.
  local clabels="${labels// /,}"
  local cmd
  if [ "$kind" = "pr" ]; then cmd=(gh pr edit "$num" --repo "$SLUG" --add-label "$clabels")
  else                     cmd=(gh issue edit "$num" --repo "$SLUG" --add-label "$clabels"); fi
  "${cmd[@]}" >/dev/null 2>&1 || true
}