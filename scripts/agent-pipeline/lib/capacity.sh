#!/usr/bin/env bash
# Capacity-aware lane gauge for the agent pipeline.
#
# HONEST LIMITATION: neither the Anthropic Max subscription nor Ollama Cloud exposes a "remaining
# % this session / this week" API. So this gauge ESTIMATES capacity from rolling run outcomes
# (usage-limit hits, recent failures, the tick self-pause window) plus a cheap health probe, and
# lets the owner pin it with an env override when they KNOW the real headroom. The >50% rule is
# applied to this estimate. See docs/agent-pipeline-routing.md §Capacity.
#
# State lives in $BASE/state/<lane>.state (key=value), NOT Redis — the agent pipeline runs on the
# host, not in the app's compose, and a flat file is trivially inspectable/repairable.
BASE="${BASE:-/home/lem/agent-pipeline}"
STATE_DIR="$BASE/state"
mkdir -p "$STATE_DIR"

# Thresholds (overridable via config.env / environment).
LANE_CAPACITY_THRESHOLD="${LANE_CAPACITY_THRESHOLD:-50}"        # >this = "available"
CLAUDE_USAGE_COOLDOWN="${CLAUDE_USAGE_COOLDOWN:-7200}"           # a usage-limit hit counts this long
CLAUDE_FAIL_WINDOW="${CLAUDE_FAIL_WINDOW:-1800}"
OLLAMA_FAIL_WINDOW="${OLLAMA_FAIL_WINDOW:-1800}"
OLLAMA_LANE_ENABLED="${OLLAMA_LANE_ENABLED:-1}"
OLLAMA_LITELLM_URL="${OLLAMA_LITELLM_URL:-http://127.0.0.1:4000}"
OLLAMA_PROBE="${OLLAMA_PROBE:-0}"   # 1 = spend a tiny completion to confirm cloud (else liveliness only)
OLLAMA_ALIAS_TTL="${OLLAMA_ALIAS_TTL:-3600}"   # how long a passing tier-alias check stays cached
OLLAMA_FAIL_PROBE_AT="${OLLAMA_FAIL_PROBE_AT:-3}"  # consecutive plain failures that trigger a probe
# Automatic usage-limit recovery (issue: Claude Max exhaustion must NOT require a human pin).
# We can't READ remaining subscription usage (no API), so we PROBE: after a usage-limit hit, the
# lane is hard-paused for USAGE_PAUSE_MINUTES; once that window passes, a cheap `claude -p "ok"`
# probe runs at most every CLAUDE_PROBE_MIN_INTERVAL. A failed probe re-pauses; a successful probe
# clears the limit marker and resumes the lane. This makes the Ollama fallback self-heal into
# Claude the moment usage resets, with zero wasted real-issue attempts.
USAGE_PAUSE_MINUTES="${USAGE_PAUSE_MINUTES:-60}"                 # pause a lane this long on a usage-limit
CLAUDE_PROBE_MIN_INTERVAL="${CLAUDE_PROBE_MIN_INTERVAL:-3600}"   # probe at most this often (seconds)
CLAUDE_PROBE_TIMEOUT="${CLAUDE_PROBE_TIMEOUT:-40}"              # the probe call's own timeout
OLLAMA_PROBE_MIN_INTERVAL="${OLLAMA_PROBE_MIN_INTERVAL:-3600}"
OLLAMA_PROBE_TIMEOUT="${OLLAMA_PROBE_TIMEOUT:-20}"
CLAUDE_PAUSED_FILE="${CLAUDE_PAUSED_FILE:-$BASE/CLAUDE_PAUSED_UNTIL}"
OLLAMA_PAUSED_FILE="${OLLAMA_PAUSED_FILE:-$BASE/OLLAMA_PAUSED_UNTIL}"
UL_REGEX='usage limit|hit your limit|limit reached|credit balance|quota exceeded|rate limit|too many requests'

_now() { date +%s; }

_state_get() {  # <lane> <key>
  local f="$STATE_DIR/$1.state" v
  [ -f "$f" ] || { echo ""; return; }
  v="$(grep -E "^$2=" "$f" 2>/dev/null | head -1 | sed -E "s/^$2=//")"
  echo "$v"
}
_state_set() {  # <lane> <key> <value>
  local f="$STATE_DIR/$1.state"
  touch "$f"
  if grep -qE "^$2=" "$f" 2>/dev/null; then
    python3 -c '
import sys
f,k,v=sys.argv[1],sys.argv[2],sys.argv[3]
lines=open(f).read().splitlines()
out=[ (k+"="+v) if l.startswith(k+"=") else l for l in lines ]
open(f,"w").write("\n".join(out)+"\n")
' "$f" "$2" "$3"
  else
    printf '%s=%s\n' "$2" "$3" >> "$f"
  fi
}

# Record a lane run outcome so the gauge learns. <lane> <ok 0|1> <usage_limit_hit 0|1>
record_lane_outcome() {
  local lane="$1" ok="$2" ul="${3:-0}"
  local now; now=$(_now)
  if [ "$ul" = "1" ]; then
    _state_set "$lane" last_usage_limit_ts "$now"
    _state_set "$lane" consecutive_failures 0
    return 0
  fi
  if [ "$ok" = "1" ]; then
    _state_set "$lane" last_success_ts "$now"
    _state_set "$lane" consecutive_failures 0
  else
    _state_set "$lane" last_fail_ts "$now"
    local cf; cf="$(_state_get "$lane" consecutive_failures)"; cf="${cf:-0}"
    _state_set "$lane" consecutive_failures "$((cf+1))"
  fi
}

# claude_capacity -> echoes "<pct> <status>"
#   status: healthy | constrained | exhausted
claude_capacity() {
  local now; now=$(_now)
  # Owner override wins when set (they know the real headroom).
  if [ -n "${CLAUDE_CAPACITY_PCT:-}" ]; then
    if [ "$CLAUDE_CAPACITY_PCT" -gt "$LANE_CAPACITY_THRESHOLD" ]; then echo "$CLAUDE_CAPACITY_PCT healthy"
    else echo "$CLAUDE_CAPACITY_PCT constrained"; fi
    return 0
  fi
  # Lane-specific hard pause (written by run_lane / the probe on a usage-limit hit) = exhausted.
  # An EXPIRED pause is cleared here and falls through to the cooldown check below — the probe
  # (run from capacity_preflight) decides whether to re-pause or resume.
  if [ -f "$CLAUDE_PAUSED_FILE" ]; then
    local pu; pu="$(cat "$CLAUDE_PAUSED_FILE" 2>/dev/null || echo 0)"
    if [ "$now" -lt "${pu:-0}" ]; then echo "0 exhausted"; return 0; fi
    rm -f "$CLAUDE_PAUSED_FILE"
  fi
  local ult; ult="$(_state_get claude last_usage_limit_ts)"; ult="${ult:-0}"
  if [ "$ult" -gt 0 ] && [ "$((now - ult))" -lt "$CLAUDE_USAGE_COOLDOWN" ]; then echo "25 constrained"; return 0; fi
  local cf; cf="$(_state_get claude consecutive_failures)"; cf="${cf:-0}"
  local lft; lft="$(_state_get claude last_fail_ts)"; lft="${lft:-0}"
  if [ "$cf" -ge 3 ] && [ "$((now - lft))" -lt "$CLAUDE_FAIL_WINDOW" ]; then echo "40 constrained"; return 0; fi
  echo "80 healthy"
}

# ollama_capacity -> echoes "<pct> <status>"
#   status: healthy | constrained | unavailable | disabled
ollama_capacity() {
  [ "$OLLAMA_LANE_ENABLED" = "1" ] || { echo "0 disabled"; return 0; }
  if [ -n "${OLLAMA_CAPACITY_PCT:-}" ]; then
    if [ "$OLLAMA_CAPACITY_PCT" -gt "$LANE_CAPACITY_THRESHOLD" ]; then echo "$OLLAMA_CAPACITY_PCT healthy"
    else echo "$OLLAMA_CAPACITY_PCT constrained"; fi
    return 0
  fi
  local now; now=$(_now)
  # Lane-specific hard pause (Ollama Max subscription usage-limit). Expired pause clears + falls through.
  if [ -f "$OLLAMA_PAUSED_FILE" ]; then
    local pu; pu="$(cat "$OLLAMA_PAUSED_FILE" 2>/dev/null || echo 0)"
    if [ "$now" -lt "${pu:-0}" ]; then echo "0 exhausted"; return 0; fi
    rm -f "$OLLAMA_PAUSED_FILE"
  fi
  # Cheap probe: LiteLLM liveliness. A down proxy = no Ollama lane.
  curl -fsS --max-time 2 "$OLLAMA_LITELLM_URL/health/liveliness" >/dev/null 2>&1 || { echo "0 unavailable"; return 0; }
  # Liveliness proves the PROXY is up — NOT that it will serve the tier alias the lane passes as
  # --model. Those came apart on 2026-07-29: proxy healthy, every run 400ing on an unresolvable
  # `lem-agent-tier*`, gauge reading 75% the whole time, so the fallback kept claiming issues and
  # stranding them `agent:working`. An unusable lane must read unavailable, not healthy.
  _ollama_alias_ok || { echo "0 unavailable"; return 0; }
  # Optional deeper probe: a 1-token completion to a cheap cloud tier (costs a fraction of a cent).
  if [ "$OLLAMA_PROBE" = "1" ] && [ -f "$BASE/secrets.env" ]; then
    . "$BASE/secrets.env" 2>/dev/null
    local body='{"model":"lem-agent-tier3","max_tokens":1,"messages":[{"role":"user","content":"ok"}]}'
    curl -fsS --max-time 15 -X POST "$OLLAMA_LITELLM_URL/v1/messages" \
      -H "x-api-key: $LITELLM_MASTER_KEY" -H "anthropic-version: 2023-06-01" \
      -H "content-type: application/json" -d "$body" >/dev/null 2>&1 || { echo "20 constrained"; return 0; }
  fi
  local cf; cf="$(_state_get ollama consecutive_failures)"; cf="${cf:-0}"
  local lft; lft="$(_state_get ollama last_fail_ts)"; lft="${lft:-0}"
  if [ "$cf" -ge 2 ] && [ "$((now - lft))" -lt "$OLLAMA_FAIL_WINDOW" ]; then echo "30 constrained"; return 0; fi
  echo "75 healthy"
}

# lane_available <pct> -> 0 when pct > threshold (available), 1 otherwise (bash truth = 0 for true)
lane_available() { [ "${1:-0}" -gt "$LANE_CAPACITY_THRESHOLD" ]; }

# ── Automatic usage-limit recovery: the Claude probe ──────────────────────────
# We cannot READ remaining Max-subscription usage (no API), so after a usage-limit hit we PROBE
# before ever sending a real issue to the Claude lane again. A cheap `claude -p "ok"` either
# succeeds (usage has reset → resume Claude) or fails with a usage-limit (still exhausted →
# re-pause). Called from capacity_preflight, so it runs once per tick but is gated to at most once
# per CLAUDE_PROBE_MIN_INTERVAL by last_probe_ts. Never runs while a hard pause is still active.
#
# Writes to the shared tick log ($_TICK_LOG) and to claude.state. Sets the CLAUDE_* env vars it
# mutates so the caller's already-computed values stay correct for the rest of the tick.
_claude_probe() {
  # Returns 0 ok, 1 usage-limit, 2 other failure. Side effect: one real (tiny) Claude call.
  local out rc
  out="$(mktemp "${TMPDIR:-/tmp}/probe.XXXXXX")"
  ( unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY 2>/dev/null || true
    timeout "${CLAUDE_PROBE_TIMEOUT:-40}" claude -p "Reply with the single word OK." \
      --dangerously-skip-permissions 2>&1 ) >"$out" 2>&1
  rc=$?
  if [ "$rc" -eq 0 ]; then rm -f "$out"; echo 0; return 0; fi
  if grep -qiE "$UL_REGEX" "$out" 2>/dev/null; then rm -f "$out"; echo 1; return 0; fi
  rm -f "$out"; echo 2; return 0
}

# Run a probe when Claude is in a post-limit state but NOT hard-paused, at most once per
# CLAUDE_PROBE_MIN_INTERVAL. On success: clear the limit marker, mark healthy. On usage-limit
# failure: re-pause CLAUDE_USAGE_PAUSE_MINUTES. Mutates CLAUDE_PCT/CLAUDE_STATUS/CLAUDE_AVAIL.
_maybe_probe_claude() {
  # Owner override = they're driving; don't second-guess it with a probe.
  [ -n "${CLAUDE_CAPACITY_PCT:-}" ] && return 0
  # Hard pause still active (claude_capacity said exhausted): nothing to probe yet.
  [ "$CLAUDE_STATUS" = "exhausted" ] && return 0
  local now ult pti
  now=$(_now)
  ult="$(_state_get claude last_usage_limit_ts)"; ult="${ult:-0}"
  [ "$ult" -gt 0 ] || return 0   # never hit a limit -> trust the gauge, no probe needed
  pti="$(_state_get claude last_probe_ts)"; pti="${pti:-0}"
  [ "$((now - pti))" -ge "${CLAUDE_PROBE_MIN_INTERVAL:-3600}" ] || return 0   # probed too recently

  local prc; prc="$(_claude_probe)"
  _state_set claude last_probe_ts "$now"
  local lg="${_TICK_LOG:-/dev/null}"
  if [ "$prc" = "0" ]; then
    _state_set claude last_usage_limit_ts 0
    CLAUDE_PCT=80; CLAUDE_STATUS=healthy
    lane_available "$CLAUDE_PCT"; CLAUDE_AVAIL=$?
    echo "[$(date '+%F %T')] claude_probe OK — Claude recovered, resuming claude lane." >>"$lg" 2>/dev/null
  else
    # Still exhausted (1) or other failure (2): re-pause so we don't keep probing every tick.
    echo "$((now + ${USAGE_PAUSE_MINUTES:-60} * 60))" > "$CLAUDE_PAUSED_FILE"
    _state_set claude last_usage_limit_ts "$now"
    CLAUDE_PCT=0; CLAUDE_STATUS=exhausted; CLAUDE_AVAIL=1
    echo "[$(date '+%F %T')] claude_probe still exhausted (rc=$prc) — pausing claude lane ${USAGE_PAUSE_MINUTES:-60}m." >>"$lg" 2>/dev/null
  fi
  export CLAUDE_PCT CLAUDE_STATUS CLAUDE_AVAIL
}

# ── Ollama lane probe (mirror of the Claude probe) ────────────────────────────
# After an Ollama-Cloud usage-limit, probe with a 1-token LiteLLM completion before dispatching a
# real issue, at most once per OLLAMA_PROBE_MIN_INTERVAL. Same recovery contract as Claude.
# Fire ONE 1-token /v1/messages call at <model> through LiteLLM and classify the result:
#   0 = served OK   1 = usage/rate limited   2 = broken (HTTP error, unknown alias, proxy down)
# Classification reads the HTTP STATUS, not curl's exit code. `curl -sS` (no -f) exits 0 on a 400,
# so the old version reported "OK" for a proxy that was rejecting the alias outright — which is how
# `anthropic_messages: Invalid model name passed in model=lem-agent-tier2` read as a healthy lane
# through 49 consecutive dead runs on 2026-07-29. Leaves a short body excerpt in
# _LITELLM_PROBE_DETAIL so the caller can log WHY, not just that it failed.
_litellm_probe() {  # $1=model alias
  [ -f "$BASE/secrets.env" ] && . "$BASE/secrets.env" 2>/dev/null
  local model="${1:-lem-agent-tier3}" out code body_ul=0
  out="$(mktemp "${TMPDIR:-/tmp}/oprobe.XXXXXX")"
  code="$(curl -sS --max-time "${OLLAMA_PROBE_TIMEOUT:-20}" -o "$out" -w '%{http_code}' \
            -X POST "$OLLAMA_LITELLM_URL/v1/messages" \
            -H "x-api-key: ${LITELLM_MASTER_KEY:-}" -H "anthropic-version: 2023-06-01" \
            -H "content-type: application/json" \
            -d "{\"model\":\"$model\",\"max_tokens\":1,\"messages\":[{\"role\":\"user\",\"content\":\"ok\"}]}" \
            2>/dev/null)"
  grep -qiE "$UL_REGEX" "$out" 2>/dev/null && body_ul=1
  # Callers read the class via $(...), i.e. from a SUBSHELL — an exported var would never reach
  # them. Park the reason in a file so the log line can say WHY the lane was held.
  printf 'http=%s %s' "$code" "$(head -c 220 "$out" 2>/dev/null | tr '\n' ' ')" > "$STATE_DIR/.litellm_probe_detail" 2>/dev/null
  rm -f "$out"
  [ "$body_ul" = "1" ] && { echo 1; return 0; }
  case "$code" in
    2*)  echo 0; return 0 ;;
    429) echo 1; return 0 ;;
  esac
  echo 2
}

# ── Ollama lane probe (mirror of the Claude probe) ────────────────────────────
# After an Ollama-Cloud usage-limit, probe with a 1-token LiteLLM completion before dispatching a
# real issue, at most once per OLLAMA_PROBE_MIN_INTERVAL. Same recovery contract as Claude.
_ollama_probe() {
  _litellm_probe "${OLLAMA_PROBE_MODEL:-${OLLAMA_DEFAULT_TIER:-lem-agent-tier3}}"
}

# Is the tier alias the lane actually passes as --model really served by LiteLLM right now?
# /health/liveliness only proves the PROXY answers; it says nothing about whether the alias
# resolves. Cached in ollama.state for OLLAMA_ALIAS_TTL so this costs ~1 token an HOUR rather than
# one per 5-minute tick. Returns 0 = alias served, 1 = not.
_ollama_alias_ok() {
  local now cached ts model prc
  now=$(_now)
  ts="$(_state_get ollama alias_ok_ts)"; ts="${ts:-0}"
  cached="$(_state_get ollama alias_ok)"
  if [ -n "$cached" ] && [ "$((now - ts))" -lt "${OLLAMA_ALIAS_TTL:-3600}" ]; then
    [ "$cached" = "1" ]
    return $?
  fi
  model="${OLLAMA_PROBE_MODEL:-${OLLAMA_DEFAULT_TIER:-lem-agent-tier2}}"
  prc="$(_litellm_probe "$model")"
  _state_set ollama alias_ok_ts "$now"
  # A usage-limit (1) is NOT an alias fault — the pause/probe path owns that. Only a hard 2 holds.
  if [ "$prc" = "2" ]; then
    _state_set ollama alias_ok 0
    echo "[$(date '+%F %T')] ollama alias probe FAILED for $model — $(cat "$STATE_DIR/.litellm_probe_detail" 2>/dev/null) — holding ollama lane." \
      >>"${_TICK_LOG:-/dev/null}" 2>/dev/null
    return 1
  fi
  _state_set ollama alias_ok 1
  return 0
}

_maybe_probe_ollama() {
  [ -n "${OLLAMA_CAPACITY_PCT:-}" ] && return 0
  [ "$OLLAMA_LANE_ENABLED" = "1" ] || return 0
  [ "$OLLAMA_STATUS" = "exhausted" ] && return 0   # hard pause still active
  local now ult pti cf
  now=$(_now)
  ult="$(_state_get ollama last_usage_limit_ts)"; ult="${ult:-0}"
  cf="$(_state_get ollama consecutive_failures)"; cf="${cf:-0}"
  # Fire after a usage-limit (the original contract) OR after a run of plain failures. A broken
  # alias, a rotated key or a bad api_base fails EVERY run without ever matching the usage-limit
  # regex, so the usage-limit-only trigger could never notice the one failure mode that had already
  # burned a day of the backlog.
  { [ "$ult" -gt 0 ] || [ "$cf" -ge "${OLLAMA_FAIL_PROBE_AT:-3}" ]; } || return 0
  pti="$(_state_get ollama last_probe_ts)"; pti="${pti:-0}"
  [ "$((now - pti))" -ge "${OLLAMA_PROBE_MIN_INTERVAL:-3600}" ] || return 0
  local prc; prc="$(_ollama_probe)"
  _state_set ollama last_probe_ts "$now"
  local lg="${_TICK_LOG:-/dev/null}"
  if [ "$prc" = "0" ]; then
    _state_set ollama last_usage_limit_ts 0
    # The probe just PROVED the lane serves, so whatever ran the failure counter up was not the
    # lane. Leaving it set would hold the gauge at "30 constrained" for the whole fail window.
    _state_set ollama consecutive_failures 0
    _state_set ollama alias_ok 1
    _state_set ollama alias_ok_ts "$now"
    OLLAMA_PCT=75; OLLAMA_STATUS=healthy
    lane_available "$OLLAMA_PCT"; OLLAMA_AVAIL=$?
    echo "[$(date '+%F %T')] ollama_probe OK — Ollama recovered, resuming ollama lane." >>"$lg" 2>/dev/null
  else
    echo "$((now + ${USAGE_PAUSE_MINUTES:-60} * 60))" > "$OLLAMA_PAUSED_FILE"
    _state_set ollama last_usage_limit_ts "$now"
    OLLAMA_PCT=0; OLLAMA_STATUS=exhausted; OLLAMA_AVAIL=1
    echo "[$(date '+%F %T')] ollama_probe still exhausted (rc=$prc) — pausing ollama lane ${USAGE_PAUSE_MINUTES:-60}m." >>"$lg" 2>/dev/null
  fi
  export OLLAMA_PCT OLLAMA_STATUS OLLAMA_AVAIL
}