#!/usr/bin/env bash
# Live operator report for the LEM agent pipeline — "what are my agents doing right now?"
#
# Read-only by design. It NEVER dispatches, probes a paid lane, mutates lane state, or writes a
# label. That matters because the obvious way to answer "is the Claude lane healthy?" is to source
# lib/capacity.sh and call claude_capacity() — but those functions have side effects: they delete an
# expired pause file, and _maybe_probe_claude() spends a real `claude -p` call. A status command the
# owner runs in a watch loop must not be able to change the thing it is reporting on, so the lane
# section RE-DERIVES status from the same state files using the same rules instead of calling in.
# The one network call it makes is LiteLLM's local /health/liveliness, which is free and idempotent.
#
# Sources of truth, in the order this reads them:
#   ps(1)                        the live `claude -p` processes = the actual running agents
#   /proc/<agent>/environ        WORKER_ID / LANE / ROUTE_REASON — slot and routing, as dispatched
#   the -p prompt itself         MODE / ISSUE / PR / BRANCH — run_lane launches with these inline
#   ~/.claude/projects/<wt>/     the agent's own transcript = what it is doing THIS minute
#   $BASE/locks/slot-*.lock      flock state = authoritative slot occupancy
#   $BASE/state/<lane>.state     lane outcome history
#   $BASE/logs/tick-outcomes.ndjson   every tick's outcome (the local mirror, works with PostHog down)
#   gh                           the backlog side: ready / working / blocked and open PRs
#
# Usage:
#   ./status.sh                 full report
#   ./status.sh --brief         agents + headline only
#   ./status.sh --watch [SECS]  refresh every SECS (default 15)
#   ./status.sh --json          machine-readable (same data, for piping/monitoring)
#   ./status.sh --hours N       window for the tick rollup (default 6)
#   ./status.sh --no-gh         skip all GitHub calls (offline / rate-limited)
#   ./status.sh --disk          also measure worktree disk usage (walks $BASE/work, slow)
set -uo pipefail

BASE="${BASE:-/home/lem/agent-pipeline}"
REPO="${REPO:-/home/lem/linkedin_engagement_manager}"
SLUG="${SLUG:-christopherqueenconsulting/linkedin_engagement_manager}"
# The pipeline always runs as `lem`, so its transcripts are under lem's home even when this report
# is run with sudo. Hard-default rather than $HOME for exactly that case.
CLAUDE_PROJECTS_DIR="${CLAUDE_PROJECTS_DIR:-/home/lem/.claude/projects}"
LOGDIR="$BASE/logs"
STATE_DIR="$BASE/state"
OUTCOMES="$LOGDIR/tick-outcomes.ndjson"

WINDOW_HOURS=6
WATCH=0
WATCH_SECS=15
JSON=0
BRIEF=0
USE_GH=1
DO_DISK=0

while [ $# -gt 0 ]; do
  case "$1" in
    --hours) WINDOW_HOURS="${2:-6}"; shift 2 ;;
    --watch) WATCH=1; case "${2:-}" in ''|-*) shift ;; *) WATCH_SECS="$2"; shift 2 ;; esac ;;
    --json)  JSON=1; shift ;;
    --brief) BRIEF=1; shift ;;
    --no-gh) USE_GH=0; shift ;;
    --disk)  DO_DISK=1; shift ;;
    -h|--help) sed -n '/^# Usage:/,/^set -uo/p' "$0" | sed 's/^# \{0,1\}//; $d'; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

# ── formatting ───────────────────────────────────────────────────────────────────────────────────
if [ -t 1 ] && [ "$JSON" = 0 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RST=$'\033[0m'; C_DIM=$'\033[2m'; C_B=$'\033[1m'
  C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_RED=$'\033[31m'; C_CYA=$'\033[36m'; C_MAG=$'\033[35m'
else
  C_RST=""; C_DIM=""; C_B=""; C_GRN=""; C_YEL=""; C_RED=""; C_CYA=""; C_MAG=""
fi

hr() { printf '%s%s%s\n' "$C_DIM" "────────────────────────────────────────────────────────────────────────────────" "$C_RST"; }
head2() { printf '\n%s%s%s\n' "$C_B$C_CYA" "$1" "$C_RST"; }

fmt_dur() {  # seconds -> compact human duration
  local s="${1:-0}"
  [ "$s" -lt 0 ] 2>/dev/null && s=0
  if   [ "$s" -lt 60 ];    then printf '%ds' "$s"
  elif [ "$s" -lt 3600 ];  then printf '%dm%02ds' $((s/60)) $((s%60))
  elif [ "$s" -lt 86400 ]; then printf '%dh%02dm' $((s/3600)) $(((s%3600)/60))
  else printf '%dd%02dh' $((s/86400)) $(((s%86400)/3600)); fi
}

# Records are split on US (0x1f), NOT tab. Tab is an IFS *whitespace* character, so `read` collapses
# a run of them into one delimiter and drops empty fields — an agent with no PR number silently
# shifts every column after it, which is exactly how the first version reported a branch name as a
# PR number. A non-whitespace delimiter makes empty fields survive.
SEP=$'\037'

NOW="$(date +%s)"
age_of() { echo $(( NOW - ${1:-0} )); }

# An absent timestamp means "this never happened", which is NOT the same as "happened at epoch 0" —
# the naive version renders a lane that has never failed as having failed 56 years ago.
age_or_never() {
  local ts="${1:-}"
  case "$ts" in ''|0) printf 'never'; return ;; esac
  printf '%s ago' "$(fmt_dur "$(age_of "$ts")")"
}

WARNINGS=()
warn() { WARNINGS+=("$1"); }

# ── config ───────────────────────────────────────────────────────────────────────────────────────
# config.env holds AGENT_GH_TOKEN in plaintext, so it is sourced in a SUBSHELL and only a whitelist
# of operational knobs is lifted out (%q-quoted). Nothing this script prints can carry the token.
load_cfg() {
  local out
  out="$(
    [ -f "$BASE/config.env" ] && . "$BASE/config.env" >/dev/null 2>&1
    for k in MAX_AGENTS SCALE_PER_ISSUES BUSY_HOURS BUSY_HOURS_UTC BUSY_TZ BUSY_DAYS BUSY_MAX_AGENTS \
             USAGE_PAUSE_MINUTES LANE_CAPACITY_THRESHOLD OLLAMA_LANE_ENABLED OLLAMA_LITELLM_URL \
             CLAUDE_CAPACITY_PCT OLLAMA_CAPACITY_PCT CLAUDE_USAGE_COOLDOWN CLAUDE_FAIL_WINDOW \
             OLLAMA_FAIL_WINDOW OLLAMA_ALIAS_TTL STALE_CLAIM_MINUTES; do
      printf 'CFG_%s=%q\n' "$k" "$(eval "printf '%s' \"\${$k:-}\"")"
    done
  )"
  eval "$out"
}
load_cfg
# config.env may define a key as empty (CLAUDE_CAPACITY_PCT= is the normal "no override" spelling),
# so every knob needs the empty case handled, not just the unset one.
CFG_MAX_AGENTS="${CFG_MAX_AGENTS:-3}";              [ -z "$CFG_MAX_AGENTS" ] && CFG_MAX_AGENTS=3
CFG_SCALE_PER_ISSUES="${CFG_SCALE_PER_ISSUES:-10}"; [ -z "$CFG_SCALE_PER_ISSUES" ] && CFG_SCALE_PER_ISSUES=10
BUSY_HOURS="${CFG_BUSY_HOURS:-${CFG_BUSY_HOURS_UTC:-}}"
BUSY_TZ="${CFG_BUSY_TZ:-UTC}"; [ -z "$BUSY_TZ" ] && BUSY_TZ=UTC
BUSY_DAYS="${CFG_BUSY_DAYS:-}"
BUSY_MAX_AGENTS="${CFG_BUSY_MAX_AGENTS:-1}"; [ -z "$BUSY_MAX_AGENTS" ] && BUSY_MAX_AGENTS=1
THRESH="${CFG_LANE_CAPACITY_THRESHOLD:-50}"; [ -z "$THRESH" ] && THRESH=50
LITELLM_URL="${CFG_OLLAMA_LITELLM_URL:-http://127.0.0.1:4000}"; [ -z "$LITELLM_URL" ] && LITELLM_URL="http://127.0.0.1:4000"
USAGE_COOLDOWN="${CFG_CLAUDE_USAGE_COOLDOWN:-7200}"; [ -z "$USAGE_COOLDOWN" ] && USAGE_COOLDOWN=7200
C_FAIL_WINDOW="${CFG_CLAUDE_FAIL_WINDOW:-1800}"; [ -z "$C_FAIL_WINDOW" ] && C_FAIL_WINDOW=1800
O_FAIL_WINDOW="${CFG_OLLAMA_FAIL_WINDOW:-1800}"; [ -z "$O_FAIL_WINDOW" ] && O_FAIL_WINDOW=1800
STALE_CLAIM_MINUTES="${CFG_STALE_CLAIM_MINUTES:-120}"; [ -z "$STALE_CLAIM_MINUTES" ] && STALE_CLAIM_MINUTES=120

# Read one variable out of a running process's environment.
#
# Read it from the AGENT process, never from its parent tick: tick.sh exports WORKER_ID (and
# run_lane sets LANE/AGENT_TIER/ROUTE_REASON) long after the tick itself was exec'd, and
# /proc/<pid>/environ is a snapshot taken AT exec — it never reflects a later export. The agent is
# exec'd after all of them are set, so its environ is the only place the slot number is legible.
env_get() {  # <pid> <KEY>
  [ -r "/proc/$1/environ" ] || return 0
  tr '\0' '\n' < "/proc/$1/environ" 2>/dev/null | grep -m1 "^$2=" | cut -d= -f2-
}

state_get() {  # <lane> <key> — same flat key=value format capacity.sh writes
  local f="$STATE_DIR/$1.state"
  [ -f "$f" ] || return 0
  grep -E "^$2=" "$f" 2>/dev/null | head -1 | sed -E "s/^$2=//"
}

# ── collection ───────────────────────────────────────────────────────────────────────────────────
AGENT_ROWS=()      # one record per pipeline agent (fields listed in render_json's `fields`)
OTHER_ROWS=()      # every OTHER claude process on the box: pid SEP label SEP elapsed
IDLE_TICKS=0       # ticks running housekeeping (merge/label/sweep) with no agent attached
PS_SNAP=""         # one ps(1) sample, shared by the agent and other-claude passes

# Pull one KEY=value out of a `claude -p` prompt. Several modes end the sentence with a period
# ("BRANCH=feature/x."), so a trailing dot is stripped — no branch or path legitimately ends in one.
prompt_field() {
  local s="$1" k="$2" v
  v="$(printf '%s' "$s" | grep -oE "(^| )$k=[^ ]+" | head -1 | sed -E "s/^ ?$k=//")"
  printf '%s' "${v%.}"
}

# Parse a `timeout 45m` argument into seconds so the report can show time left before the kill.
to_seconds() {
  # Split, not one `local d=... n=${d%...}`: bash expands every word on the line BEFORE `local`
  # assigns any of them, so the derived values would read an unset `d` and trip `set -u`.
  local d="${1:-}" n u
  n="${d%[smhd]}"; u="${d##*[0-9]}"
  case "$d" in ''|*[!0-9smhd]*) echo 0; return ;; esac
  [ -z "$n" ] && { echo 0; return; }
  case "$u" in s|'') echo $((n)) ;; m) echo $((n*60)) ;; h) echo $((n*3600)) ;; d) echo $((n*86400)) ;; *) echo 0 ;; esac
}

# The agent's own transcript is the only place that says what it is doing RIGHT NOW — the tick log
# only records dispatch and outcome. Read the tail so a 400KB session costs nothing.
last_activity() {  # <worktree> -> "<age_seconds><SEP><what it last did>"
  local wt="$1" dir f age
  [ -n "$wt" ] || { printf '%s' "$SEP"; return; }
  dir="$CLAUDE_PROJECTS_DIR/$(printf '%s' "$wt" | tr '/' '-')"
  [ -d "$dir" ] || { printf '%s' "$SEP"; return; }
  f="$(ls -t "$dir"/*.jsonl 2>/dev/null | head -1)"
  [ -n "$f" ] || { printf '%s' "$SEP"; return; }
  age=$(( NOW - $(stat -c %Y "$f" 2>/dev/null || echo "$NOW") ))
  printf '%s%s%s' "$age" "$SEP" "$(tail -c 300000 "$f" 2>/dev/null | tail -n 60 | python3 -c '
import json, sys
last = ""
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        o = json.loads(line)
    except Exception:
        continue
    msg = o.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                i = b.get("input") or {}
                d = (i.get("command") or i.get("file_path") or i.get("pattern")
                     or i.get("description") or i.get("prompt") or i.get("body") or "")
                last = "%s: %s" % (b.get("name", "tool"), " ".join(str(d).split()))
            elif b.get("type") == "text" and b.get("text", "").strip():
                last = "says: " + " ".join(b["text"].split())
    elif isinstance(content, str) and content.strip() and msg.get("role") == "assistant":
        last = "says: " + " ".join(content.split())
print(last[:150])
' 2>/dev/null | tr '\t\037' '  ')"
}

collect_agents() {
  local snap pid ppid etimes args
  snap="$(ps -eo pid=,ppid=,etimes=,args= 2>/dev/null)"
  [ -n "$snap" ] || return 0
  PS_SNAP="$snap"   # reused by collect_other_claude so the two sections read one consistent sample

  declare -A P_PPID P_ARGS P_ET
  local -a tick_pids=()
  while read -r pid ppid etimes args; do
    [ -n "$pid" ] || continue
    P_PPID[$pid]="$ppid"; P_ARGS[$pid]="$args"; P_ET[$pid]="$etimes"
    # A cron tick appears twice: the `/bin/sh -c ...` wrapper and the real `bash tick.sh`. Keep the
    # latter — it is the process that holds the slot flock and carries WORKER_ID in its environ.
    case "$args" in
      *"$BASE/tick.sh"*) case "$args" in *" -c "*) : ;; *) tick_pids+=("$pid") ;; esac ;;
    esac
  done <<<"$snap"

  local -A tick_has_agent=()
  local apid tpid hop slot mode issue pr branch lane model route wt risk attempts elapsed tmo remain act tr_age
  for apid in "${!P_ARGS[@]}"; do
    args="${P_ARGS[$apid]}"
    # The agent process itself, not the `timeout 45m claude -p ...` wrapper that owns it (both carry
    # identical prompt text, so matching on the prompt alone double-counts every agent).
    case "$args" in
      claude\ -p\ *|*/claude\ -p\ *) : ;;
      *) continue ;;
    esac
    case "$args" in *MODE=*) : ;; *) continue ;; esac

    # Walk up to the owning tick to learn the slot. Depth is small (tick -> timeout -> claude).
    tpid=""; hop="${P_PPID[$apid]:-1}"
    for _ in 1 2 3 4 5 6; do
      [ -n "$hop" ] && [ "$hop" != "1" ] || break
      case "${P_ARGS[$hop]:-}" in
        *"$BASE/tick.sh"*) case "${P_ARGS[$hop]}" in *" -c "*) : ;; *) tpid="$hop"; break ;; esac ;;
      esac
      hop="${P_PPID[$hop]:-1}"
    done
    [ -n "$tpid" ] && tick_has_agent[$tpid]=1

    slot="$(env_get "$apid" WORKER_ID)"
    route="$(env_get "$apid" ROUTE_REASON)"

    mode="$(prompt_field "$args" MODE)"
    issue="$(prompt_field "$args" ISSUE)"
    pr="$(prompt_field "$args" PR)"
    branch="$(prompt_field "$args" BRANCH)"
    risk="$(prompt_field "$args" RISK)"
    attempts="$(prompt_field "$args" ATTEMPTS)"
    wt="$(prompt_field "$args" WORKTREE)"
    # Only MODE=start passes WORKTREE in the prompt; every other mode works in $WORKROOT/<branch>.
    [ -z "$wt" ] && [ -n "$branch" ] && wt="$BASE/work/$branch"

    # Prefer what run_lane actually decided (its environ). Fall back to the `--model` flag on the
    # command line, whose shape is the lane tell: lem-agent-tier* = LiteLLM/Ollama, else Claude Max.
    lane="$(env_get "$apid" LANE)"
    model="$(env_get "$apid" AGENT_TIER)"
    [ -z "$model" ] && model="$(env_get "$apid" AGENT_MODEL)"
    [ -z "$model" ] && model="$(printf '%s' "$args" | grep -oE ' --model [^ ]+' | head -1 | awk '{print $2}')"
    if [ -z "$lane" ]; then
      case "$model" in
        lem-agent-tier*|lem-*) lane="ollama" ;;
        "")                    lane="claude" ;;
        *)                     lane="claude" ;;
      esac
    fi
    [ -z "$model" ] && model="default"

    elapsed="${P_ET[$apid]:-0}"
    tmo=0
    [ -n "${P_PPID[$apid]:-}" ] && case "${P_ARGS[${P_PPID[$apid]}]:-}" in
      timeout\ *) tmo="$(to_seconds "$(printf '%s' "${P_ARGS[${P_PPID[$apid]}]}" | awk '{print $2}')")" ;;
    esac
    remain=$(( tmo - elapsed )); [ "$remain" -lt 0 ] && remain=0

    IFS="$SEP" read -r tr_age act <<<"$(last_activity "$wt")"
    tr_age="${tr_age:-}"; act="${act:-}"

    AGENT_ROWS+=("${apid}${SEP}${tpid:-}${SEP}${slot:-?}${SEP}${mode:-?}${SEP}${issue:-}${SEP}${pr:-}${SEP}${branch:-}${SEP}${lane}${SEP}${model:-}${SEP}${route:-}${SEP}${elapsed}${SEP}${tmo}${SEP}${remain}${SEP}${risk:-}${SEP}${attempts:-}${SEP}${wt:-}${SEP}${tr_age:-}${SEP}${act:-}")

    [ "$tmo" -gt 0 ] && [ "$remain" -gt 0 ] && [ "$remain" -lt 300 ] && \
      warn "agent pid $apid (${mode:-?} ${issue:+#$issue}${pr:+PR #$pr}) is $(fmt_dur "$remain") from its ${tmo}s timeout — it will be killed mid-run and retried next tick"
    [ -n "$tr_age" ] && [ "$tr_age" -gt 900 ] 2>/dev/null && \
      warn "agent pid $apid (${mode:-?} ${issue:+#$issue}${pr:+PR #$pr}) has written nothing to its transcript for $(fmt_dur "$tr_age") — possibly hung"
  done

  for tpid in "${tick_pids[@]+"${tick_pids[@]}"}"; do
    [ -n "${tick_has_agent[$tpid]:-}" ] || IDLE_TICKS=$((IDLE_TICKS+1))
  done

  # Associative-array iteration order is a hash order, so without this the agent list reshuffles on
  # every refresh — unreadable in --watch, where the whole point is spotting what changed.
  if [ "${#AGENT_ROWS[@]}" -gt 1 ]; then
    mapfile -t AGENT_ROWS < <(printf '%s\n' "${AGENT_ROWS[@]}" | sort -t"$SEP" -k3,3 -k1,1n)
  fi
}

# Every OTHER Claude process on the box — an interactive session, the tmux remote-control session, a
# headless run someone started by hand. They are NOT pipeline agents and work no issue, but they draw
# on the SAME Max subscription as the claude lane, so "how many Claude instances are running" is
# wrong without them. This is also the answer to "why did the lane hit a usage limit at 2am".
collect_other_claude() {
  local pid ppid etimes args label is_claude parent_args
  local agent_pids=" $(printf '%s\n' "${AGENT_ROWS[@]+"${AGENT_ROWS[@]}"}" | cut -d"$SEP" -f1 | tr '\n' ' ') "
  while read -r pid ppid etimes args; do
    [ -n "$pid" ] || continue
    case "$agent_pids" in *" $pid "*) continue ;; esac
    is_claude=0
    case "$args" in
      claude|claude\ *|*/claude|*/claude\ *) is_claude=1 ;;
      */share/claude/versions/*)             is_claude=1 ;;
    esac
    [ "$is_claude" = 1 ] || continue
    # A session's own model-driver child (…/versions/2.1.x --print --sdk-url …) is one instance with
    # its parent, not two. Collapse anything whose parent is itself a claude process.
    parent_args="$(printf '%s\n' "$PS_SNAP" | awk -v p="$ppid" '$1==p {$1="";$2="";$3="";print;exit}')"
    case "$parent_args" in
      *claude|*claude\ *|*/share/claude/versions/*) continue ;;
    esac
    case "$args" in
      *" remote-control"*) label="remote-control session" ;;
      *" -p "*|*" --print"*) label="headless run (not the pipeline)" ;;
      *) label="interactive session" ;;
    esac
    OTHER_ROWS+=("${pid}${SEP}${label}${SEP}${etimes}")
  done <<<"$PS_SNAP"
}

# Slot occupancy straight from the flocks tick.sh holds — the authoritative answer, independent of
# how many processes ps happens to show.
SLOTS_BUSY=""; SLOTS_FREE=""; SLOTS_SEEN=0
collect_slots() {
  local f n
  command -v flock >/dev/null 2>&1 || return 0
  for f in "$BASE"/locks/slot-*.lock; do
    [ -e "$f" ] || continue
    n="$(basename "$f" .lock)"; n="${n#slot-}"
    SLOTS_SEEN=$((SLOTS_SEEN+1))
    # Append-open so testing a lock can never truncate or create state tick.sh relies on.
    if ( exec 9>>"$f"; flock -n 9 ) 2>/dev/null; then SLOTS_FREE="$SLOTS_FREE $n"; else SLOTS_BUSY="$SLOTS_BUSY $n"; fi
  done
}

# Replicates tick.sh's cap arithmetic (1 + ready/SCALE, ceilinged by MAX_AGENTS, then by the busy
# window). Kept in step by hand: if tick.sh's formula changes, this line has to change with it.
CAP=1; BUSY_WINDOW="no"
compute_cap() {
  local ready="${1:-0}" h dow bs be ind inh
  CAP=$(( 1 + ready / CFG_SCALE_PER_ISSUES ))
  [ "$CAP" -gt "$CFG_MAX_AGENTS" ] && CAP="$CFG_MAX_AGENTS"
  [ -n "$BUSY_HOURS" ] || return 0
  h=$((10#$(TZ="$BUSY_TZ" date +%H))); dow=$(TZ="$BUSY_TZ" date +%u)
  bs="${BUSY_HOURS%-*}"; be="${BUSY_HOURS#*-}"; inh=0
  if { [ "$bs" -le "$be" ] && [ "$h" -ge "$bs" ] && [ "$h" -lt "$be" ]; } \
     || { [ "$bs" -gt "$be" ] && { [ "$h" -ge "$bs" ] || [ "$h" -lt "$be" ]; }; }; then inh=1; fi
  ind=1
  if [ -n "$BUSY_DAYS" ]; then
    [ "$dow" -ge "${BUSY_DAYS%-*}" ] && [ "$dow" -le "${BUSY_DAYS#*-}" ] || ind=0
  fi
  if [ "$inh" = 1 ] && [ "$ind" = 1 ]; then
    BUSY_WINDOW="yes"
    [ "$CAP" -gt "$BUSY_MAX_AGENTS" ] && CAP="$BUSY_MAX_AGENTS"
  fi
}

# Lane status, re-derived read-only. Mirrors capacity.sh's claude_capacity/ollama_capacity rules
# WITHOUT their side effects (no pause-file deletion, no billable probe).
CL_PCT=0; CL_STATUS="?"; CL_NOTE=""
OL_PCT=0; OL_STATUS="?"; OL_NOTE=""
collect_lanes() {
  local pu ult cf lft rem

  if [ -n "${CFG_CLAUDE_CAPACITY_PCT:-}" ]; then
    CL_PCT="$CFG_CLAUDE_CAPACITY_PCT"
    [ "$CL_PCT" -gt "$THRESH" ] && CL_STATUS="healthy" || CL_STATUS="constrained"
    CL_NOTE="owner override (CLAUDE_CAPACITY_PCT)"
  elif [ -f "$BASE/CLAUDE_PAUSED_UNTIL" ] && pu="$(cat "$BASE/CLAUDE_PAUSED_UNTIL" 2>/dev/null)" && [ "${pu:-0}" -gt "$NOW" ] 2>/dev/null; then
    rem=$(( pu - NOW )); CL_PCT=0; CL_STATUS="exhausted"; CL_NOTE="hard pause, $(fmt_dur "$rem") left"
    warn "claude lane is PAUSED for another $(fmt_dur "$rem") (usage limit) — Ollama is carrying the backlog"
  else
    [ -f "$BASE/CLAUDE_PAUSED_UNTIL" ] && CL_NOTE="pause expired, next tick probes to resume"
    ult="$(state_get claude last_usage_limit_ts)"; ult="${ult:-0}"
    cf="$(state_get claude consecutive_failures)"; cf="${cf:-0}"
    lft="$(state_get claude last_fail_ts)"; lft="${lft:-0}"
    if [ "$ult" -gt 0 ] && [ "$(( NOW - ult ))" -lt "$USAGE_COOLDOWN" ]; then
      CL_PCT=25; CL_STATUS="constrained"; CL_NOTE="usage limit $(fmt_dur "$(age_of "$ult")") ago (cooldown)"
    elif [ "$cf" -ge 3 ] && [ "$(( NOW - lft ))" -lt "$C_FAIL_WINDOW" ]; then
      CL_PCT=40; CL_STATUS="constrained"; CL_NOTE="$cf consecutive failures"
    else
      CL_PCT=80; CL_STATUS="healthy"
    fi
  fi

  if [ "${CFG_OLLAMA_LANE_ENABLED:-1}" != "1" ]; then
    OL_PCT=0; OL_STATUS="disabled"; OL_NOTE="OLLAMA_LANE_ENABLED=0"
  elif [ -n "${CFG_OLLAMA_CAPACITY_PCT:-}" ]; then
    OL_PCT="$CFG_OLLAMA_CAPACITY_PCT"
    [ "$OL_PCT" -gt "$THRESH" ] && OL_STATUS="healthy" || OL_STATUS="constrained"
    OL_NOTE="owner override (OLLAMA_CAPACITY_PCT)"
  elif [ -f "$BASE/OLLAMA_PAUSED_UNTIL" ] && pu="$(cat "$BASE/OLLAMA_PAUSED_UNTIL" 2>/dev/null)" && [ "${pu:-0}" -gt "$NOW" ] 2>/dev/null; then
    rem=$(( pu - NOW )); OL_PCT=0; OL_STATUS="exhausted"; OL_NOTE="hard pause, $(fmt_dur "$rem") left"
  elif ! curl -fsS --max-time 2 "$LITELLM_URL/health/liveliness" >/dev/null 2>&1; then
    OL_PCT=0; OL_STATUS="unavailable"; OL_NOTE="LiteLLM not answering at $LITELLM_URL"
    warn "LiteLLM is not answering on $LITELLM_URL — the ollama lane is dead until it is back"
  elif [ "$(state_get ollama alias_ok)" = "0" ]; then
    # The 2026-07-29 failure: proxy healthy, every run 400ing on an unresolvable tier alias.
    OL_PCT=0; OL_STATUS="unavailable"; OL_NOTE="tier alias not served — $(cat "$STATE_DIR/.litellm_probe_detail" 2>/dev/null | head -c 90)"
    warn "ollama tier alias is NOT being served by LiteLLM (proxy is up but runs would 400)"
  else
    cf="$(state_get ollama consecutive_failures)"; cf="${cf:-0}"
    lft="$(state_get ollama last_fail_ts)"; lft="${lft:-0}"
    if [ "$cf" -ge 2 ] && [ "$(( NOW - lft ))" -lt "$O_FAIL_WINDOW" ]; then
      OL_PCT=30; OL_STATUS="constrained"; OL_NOTE="$cf consecutive failures"
    else
      OL_PCT=75; OL_STATUS="healthy"
    fi
  fi

  { [ "$CL_PCT" -le "$THRESH" ] && [ "$OL_PCT" -le "$THRESH" ]; } && \
    warn "BOTH lanes at or below the ${THRESH}% threshold — pipeline is DEGRADED: cap forced to 1 and new issue starts are held"
}

# ── GitHub backlog ───────────────────────────────────────────────────────────────────────────────
GH_OK=0; GH_READY=0; GH_WORKING=""; GH_BLOCKED=""; GH_HUMAN=""; GH_PRS=""
collect_gh() {
  [ "$USE_GH" = 1 ] || return 0
  command -v gh >/dev/null 2>&1 || return 0
  local td; td="$(mktemp -d)"
  # Fired concurrently: five sequential gh round-trips is the slowest part of this report.
  gh issue list --repo "$SLUG" --state open --limit 100 --label "agent:ready" \
     --json number --jq 'length' >"$td/ready" 2>/dev/null &
  gh issue list --repo "$SLUG" --state open --limit 100 --label "agent:working" \
     --json number,title,updatedAt --jq '.[] | "\(.number)\u001f\(.updatedAt)\u001f\(.title)"' >"$td/working" 2>/dev/null &
  gh issue list --repo "$SLUG" --state open --limit 100 --label "agent:blocked" \
     --json number,title --jq '.[] | "\(.number)\u001f\(.title)"' >"$td/blocked" 2>/dev/null &
  gh issue list --repo "$SLUG" --state open --limit 100 --label "needs-human" \
     --json number,title --jq '.[] | "\(.number)\u001f\(.title)"' >"$td/human" 2>/dev/null &
  # The labels field is empty on most PRs — hence \u001f (US) here too, or the title shifts into it.
  gh pr list --repo "$SLUG" --state open --limit 100 \
     --json number,title,isDraft,mergeStateStatus,headRefName,updatedAt,labels \
     --jq '.[] | "\(.number)\u001f\(.headRefName)\u001f\(.mergeStateStatus)\u001f\(.isDraft)\u001f\(.updatedAt)\u001f\([.labels[].name] | map(select(startswith("agent:") or startswith("risk:") or . == "needs-human")) | join(","))\u001f\(.title)"' \
     >"$td/prs" 2>/dev/null &
  wait
  GH_READY="$(cat "$td/ready" 2>/dev/null)"; GH_READY="${GH_READY:-0}"
  GH_WORKING="$(cat "$td/working" 2>/dev/null)"
  GH_BLOCKED="$(cat "$td/blocked" 2>/dev/null)"
  GH_HUMAN="$(cat "$td/human" 2>/dev/null)"
  GH_PRS="$(cat "$td/prs" 2>/dev/null)"
  [ -s "$td/ready" ] && GH_OK=1
  rm -rf "$td"

  # A claim with no agent behind it is the failure that strands an issue: `agent:working` stays on,
  # so nothing else picks it up, while no process is actually on it. tick.sh re-queues after
  # STALE_CLAIM_MINUTES, so only flag claims older than that.
  local n u t running age
  running=" $(printf '%s\n' "${AGENT_ROWS[@]+"${AGENT_ROWS[@]}"}" | cut -d"$SEP" -f5 | tr '\n' ' ') "
  while IFS="$SEP" read -r n u t; do
    [ -n "$n" ] || continue
    case "$running" in *" $n "*) continue ;; esac
    age=$(( NOW - $(date -d "$u" +%s 2>/dev/null || echo "$NOW") ))
    [ "$age" -gt $(( STALE_CLAIM_MINUTES * 60 )) ] && \
      warn "issue #$n is labelled agent:working with NO agent running and no update for $(fmt_dur "$age") — likely a stranded claim"
  done <<<"$GH_WORKING"
}

# ── tick rollup ──────────────────────────────────────────────────────────────────────────────────
ROLLUP=""; LAST_TICK_AGE=-1
collect_rollup() {
  [ -f "$OUTCOMES" ] || return 0
  # Bounded read: the file is append-only and grows forever; the window of interest is hours.
  ROLLUP="$(tail -n 4000 "$OUTCOMES" 2>/dev/null | WINDOW_HOURS="$WINDOW_HOURS" python3 -c '
import calendar, json, os, sys, time
from collections import Counter

window = float(os.environ.get("WINDOW_HOURS", "6")) * 3600
now = time.time()
outcomes, modes, reasons, pr_fail = Counter(), Counter(), Counter(), Counter()
total = 0
newest = 0.0
dur = []
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        o = json.loads(line)
    except Exception:
        continue
    ts = o.get("ts") or ""
    try:
        # timegm, not mktime-minus-timezone: the stamp is UTC, and mktime reinterprets it as local
        # time with a guessed DST flag. Identical on this UTC box, an hour out on any other.
        t = calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        continue
    newest = max(newest, t)
    if now - t > window:
        continue
    total += 1
    oc = o.get("tick_outcome") or "unknown"
    outcomes[oc] += 1
    if oc == "dispatched":
        modes[o.get("mode") or "?"] += 1
        if o.get("duration_ms"):
            dur.append(o["duration_ms"] / 1000.0)
    else:
        r = o.get("reason") or ""
        if r:
            reasons["%s/%s" % (oc, r)] += 1
    if oc == "failed" and o.get("pr_number"):
        pr_fail[o["pr_number"]] += 1

print("TOTAL=%d" % total)
print("AGE=%d" % (int(now - newest) if newest else -1))
print("OUTCOMES=" + ",".join("%s:%d" % kv for kv in outcomes.most_common()))
print("MODES=" + ",".join("%s:%d" % kv for kv in modes.most_common()))
print("REASONS=" + ",".join("%s:%d" % kv for kv in reasons.most_common(6)))
print("AVGDUR=%d" % (sum(dur) / len(dur) if dur else 0))
print("PRFAIL=" + ",".join("%s:%d" % kv for kv in pr_fail.most_common(3) if kv[1] >= 3))
' 2>/dev/null)"
  LAST_TICK_AGE="$(printf '%s\n' "$ROLLUP" | grep -m1 '^AGE=' | cut -d= -f2)"
  LAST_TICK_AGE="${LAST_TICK_AGE:--1}"
  # cron fires every 5 minutes; a tick that has not landed in 15 is cron or the runner being dead.
  [ "$LAST_TICK_AGE" -ge 0 ] && [ "$LAST_TICK_AGE" -gt 900 ] && \
    warn "no tick outcome recorded for $(fmt_dur "$LAST_TICK_AGE") — cron may not be firing (expected every 5m)"
  local prfail; prfail="$(printf '%s\n' "$ROLLUP" | grep -m1 '^PRFAIL=' | cut -d= -f2)"
  [ -n "$prfail" ] && warn "merge keeps failing on the same PR(s): $prfail (count in the last ${WINDOW_HOURS}h)"
}
rollup_get() { printf '%s\n' "$ROLLUP" | grep -m1 "^$1=" | cut -d= -f2-; }

# ── host + repo ──────────────────────────────────────────────────────────────────────────────────
WT_COUNT=0; DISK_FREE=""; DISK_PCT=""; LOADAVG=""; MEM=""; WORK_DU=""
collect_host() {
  WT_COUNT="$(git -C "$REPO" worktree list 2>/dev/null | wc -l)"
  DISK_FREE="$(df -h "$BASE" 2>/dev/null | awk 'NR==2{print $4}')"
  DISK_PCT="$(df -P "$BASE" 2>/dev/null | awk 'NR==2{gsub("%","",$5); print $5}')"
  LOADAVG="$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)"
  MEM="$(free -h 2>/dev/null | awk 'NR==2{print $3" used / "$2}')"
  [ "$DO_DISK" = 1 ] && WORK_DU="$(du -sh "$BASE/work" 2>/dev/null | cut -f1)"
  [ -n "$DISK_PCT" ] && [ "$DISK_PCT" -ge 85 ] 2>/dev/null && \
    warn "disk is ${DISK_PCT}% full — worktrees accumulate fast (sweep runs hourly; --disk shows the size)"
}

collect_pipeline_state() {
  [ -f "$BASE/PAUSED" ] && warn "pipeline is PAUSED ($BASE/PAUSED exists) — no tick will dispatch until it is removed"
  crontab -l 2>/dev/null | grep -qF "$BASE/tick.sh" || \
    warn "no crontab entry for $BASE/tick.sh — nothing is driving the pipeline on a schedule"
}

# ── renderers ────────────────────────────────────────────────────────────────────────────────────
render_text() {
  local n_agents="${#AGENT_ROWS[@]}"
  printf '%s%s%s  %s%s%s\n' "$C_B" "LEM agent pipeline" "$C_RST" "$C_DIM" "$(hostname) · $(date '+%F %T %Z')" "$C_RST"
  hr
  local paused_txt="running"
  [ -f "$BASE/PAUSED" ] && paused_txt="${C_RED}PAUSED${C_RST}"
  printf '  %-14s %s   %-14s %s\n' "pipeline:" "$paused_txt" "agents live:" \
    "${C_B}${n_agents}${C_RST}/${CAP}$([ "$BUSY_WINDOW" = yes ] && printf ' %s(busy window)%s' "$C_YEL" "$C_RST")$([ "${#OTHER_ROWS[@]}" -gt 0 ] && printf ' %s(+%d other claude)%s' "$C_DIM" "${#OTHER_ROWS[@]}" "$C_RST")"
  printf '  %-14s %s   %-14s %s\n' "backlog:" "$([ "$GH_OK" = 1 ] && echo "${GH_READY} agent:ready" || echo "${C_DIM}n/a${C_RST}")" \
         "last tick:" "$([ "${LAST_TICK_AGE}" -ge 0 ] 2>/dev/null && fmt_dur "$LAST_TICK_AGE" || echo "?") ago"
  printf '  %-14s claude %s%d%%%s %-13s ollama %s%d%%%s %s\n' "lanes:" \
    "$([ "$CL_PCT" -gt "$THRESH" ] && echo "$C_GRN" || echo "$C_YEL")" "$CL_PCT" "$C_RST" "($CL_STATUS)" \
    "$([ "$OL_PCT" -gt "$THRESH" ] && echo "$C_GRN" || echo "$C_YEL")" "$OL_PCT" "$C_RST" "($OL_STATUS)"

  head2 "RUNNING AGENTS ($n_agents)"
  if [ "$n_agents" = 0 ]; then
    printf '  %sno agent processes — %s%s\n' "$C_DIM" \
      "$([ -f "$BASE/PAUSED" ] && echo "pipeline is paused" || echo "ticks are idle or doing housekeeping (merge/label/sweep)")" "$C_RST"
  else
    local pid tpid slot mode issue pr branch lane model route elapsed tmo remain risk attempts wt tr_age act target row
    printf '  %s%-4s %-8s %-11s %-14s %-30s %-8s %-14s %s%s\n' "$C_DIM" "SLOT" "PID" "MODE" "TARGET" "BRANCH" "RUNTIME" "LANE/MODEL" "LEFT" "$C_RST"
    for row in "${AGENT_ROWS[@]}"; do
      IFS="$SEP" read -r pid tpid slot mode issue pr branch lane model route elapsed tmo remain risk attempts wt tr_age act <<<"$row"
      target=""
      [ -n "$issue" ] && target="#$issue"
      [ -n "$pr" ] && target="${target:+$target }PR#$pr"
      [ -n "$attempts" ] && target="$target try$attempts"
      printf '  %-4s %-8s %s%-11s%s %-14s %-30s %-8s %-14s %s\n' \
        "${slot:-?}" "$pid" "$C_MAG" "$mode" "$C_RST" "${target:-—}" "${branch:0:30}" \
        "$(fmt_dur "$elapsed")" "$lane/${model#lem-agent-}" \
        "$([ "$tmo" -gt 0 ] && fmt_dur "$remain" || echo '—')"
      if [ -n "$act" ]; then
        printf '        %s└ %s  %s%s\n' "$C_DIM" "${act:0:110}" \
          "$([ -n "$tr_age" ] && printf '[%s ago]' "$(fmt_dur "$tr_age")")" "$C_RST"
      fi
    done
    [ "$IDLE_TICKS" -gt 0 ] && printf '  %s+ %d tick(s) running housekeeping with no agent attached%s\n' "$C_DIM" "$IDLE_TICKS" "$C_RST"
  fi

  if [ "${#OTHER_ROWS[@]}" -gt 0 ]; then
    local opid olabel oelapsed orow
    printf '\n  %sother claude processes (%d) — no issue, but same Max subscription as the claude lane:%s\n' \
      "$C_DIM" "${#OTHER_ROWS[@]}" "$C_RST"
    for orow in "${OTHER_ROWS[@]}"; do
      IFS="$SEP" read -r opid olabel oelapsed <<<"$orow"
      printf '    %s%-8s %-34s up %s%s\n' "$C_DIM" "$opid" "$olabel" "$(fmt_dur "$oelapsed")" "$C_RST"
    done
  fi

  [ "$BRIEF" = 1 ] && { render_warnings; return; }

  head2 "SLOTS & CAPACITY"
  printf '  cap %s%s%s of MAX_AGENTS=%s   (1 + ready/%s)%s\n' "$C_B" "$CAP" "$C_RST" "$CFG_MAX_AGENTS" "$CFG_SCALE_PER_ISSUES" \
    "$([ "$BUSY_WINDOW" = yes ] && printf ' — busy window %s %s%s limits to %s' "$BUSY_HOURS" "$BUSY_TZ" "${BUSY_DAYS:+ days $BUSY_DAYS}" "$BUSY_MAX_AGENTS")"
  printf '  locks held:%s   free:%s\n' "${SLOTS_BUSY:- none}" "${SLOTS_FREE:- none}"
  printf '  claude  %3d%% %-12s %s\n' "$CL_PCT" "$CL_STATUS" "$C_DIM${CL_NOTE}$C_RST"
  printf '    %slast success %s · last failure %s%s\n' "$C_DIM" \
    "$(age_or_never "$(state_get claude last_success_ts)")" "$(age_or_never "$(state_get claude last_fail_ts)")" "$C_RST"
  printf '  ollama  %3d%% %-12s %s\n' "$OL_PCT" "$OL_STATUS" "$C_DIM${OL_NOTE}$C_RST"
  printf '    %slast success %s · last failure %s%s\n' "$C_DIM" \
    "$(age_or_never "$(state_get ollama last_success_ts)")" "$(age_or_never "$(state_get ollama last_fail_ts)")" "$C_RST"

  if [ "$GH_OK" = 1 ]; then
    head2 "BACKLOG"
    printf '  %-14s %s\n' "agent:ready" "$GH_READY"
    local n t u rest
    printf '  %-14s %s\n' "agent:working" "$(printf '%s' "$GH_WORKING" | grep -c . )"
    while IFS="$SEP" read -r n u t; do
      [ -n "$n" ] || continue
      printf '      %s#%-6s%s %s%s\n' "$C_CYA" "$n" "$C_RST" "${t:0:70}" \
        "$(printf '%s\n' "${AGENT_ROWS[@]+"${AGENT_ROWS[@]}"}" | cut -d"$SEP" -f5 | grep -qx "$n" && printf ' %s← live%s' "$C_GRN" "$C_RST" || printf ' %s(no agent)%s' "$C_YEL" "$C_RST")"
    done <<<"$GH_WORKING"
    if [ -n "$GH_BLOCKED" ]; then
      printf '  %-14s %s\n' "agent:blocked" "$(printf '%s' "$GH_BLOCKED" | grep -c .)"
      while IFS="$SEP" read -r n t; do [ -n "$n" ] && printf '      %s#%-6s%s %s\n' "$C_YEL" "$n" "$C_RST" "${t:0:70}"; done <<<"$GH_BLOCKED"
    fi
    if [ -n "$GH_HUMAN" ]; then
      printf '  %-14s %s\n' "needs-human" "$(printf '%s' "$GH_HUMAN" | grep -c .)"
      while IFS="$SEP" read -r n t; do [ -n "$n" ] && printf '      %s#%-6s%s %s\n' "$C_RED" "$n" "$C_RST" "${t:0:70}"; done <<<"$GH_HUMAN"
    fi

    head2 "OPEN PRs ($(printf '%s' "$GH_PRS" | grep -c .))"
    local pn br st draft up labs title
    while IFS="$SEP" read -r pn br st draft up labs title; do
      [ -n "$pn" ] || continue
      local col="$C_RST"
      case "$st" in CLEAN) col="$C_GRN" ;; BLOCKED|DIRTY) col="$C_RED" ;; BEHIND|UNSTABLE) col="$C_YEL" ;; esac
      printf '  %s#%-6s%s %s%-10s%s %-30s %s%s\n' "$C_CYA" "$pn" "$C_RST" "$col" "${st:0:10}" "$C_RST" "${br:0:30}" \
        "${title:0:44}" "$([ -n "$labs" ] && printf ' %s[%s]%s' "$C_DIM" "$labs" "$C_RST")"
    done <<<"$GH_PRS"
  fi

  head2 "LAST ${WINDOW_HOURS}h OF TICKS"
  printf '  %-14s %s ticks · avg dispatch %ss\n' "volume:" "$(rollup_get TOTAL)" "$(rollup_get AVGDUR)"
  printf '  %-14s %s\n' "outcomes:" "$(rollup_get OUTCOMES | tr ',' ' ')"
  printf '  %-14s %s\n' "work done:" "$(rollup_get MODES | tr ',' ' ')"
  printf '  %-14s %s\n' "top reasons:" "$(rollup_get REASONS | tr ',' ' ')"

  head2 "RECENT PIPELINE LOG"
  local today="$LOGDIR/tick-$(date +%Y%m%d).log"
  if [ -f "$today" ]; then
    grep -iE "REFUSING|FAILED|error|usage/rate limit|stuck|escalat|blocked|held" "$today" 2>/dev/null | tail -6 \
      | sed "s/^/  $C_DIM/; s/$/$C_RST/"
    printf '  %s(full: tail -f %s)%s\n' "$C_DIM" "$today" "$C_RST"
  else
    printf '  %sno log for today yet: %s%s\n' "$C_DIM" "$today" "$C_RST"
  fi

  head2 "HOST"
  printf '  %-14s %s worktrees%s   %-10s %s free (%s%% used)\n' "repo:" "$WT_COUNT" \
    "$([ -n "$WORK_DU" ] && printf ' (%s on disk)' "$WORK_DU")" "disk:" "$DISK_FREE" "$DISK_PCT"
  printf '  %-14s %s   %-10s %s\n' "load:" "$LOADAVG" "mem:" "$MEM"

  render_warnings
}

render_warnings() {
  printf '\n'
  if [ "${#WARNINGS[@]}" = 0 ]; then
    printf '%s✓ nothing needs your attention%s\n' "$C_GRN" "$C_RST"
  else
    printf '%s%s⚠ NEEDS ATTENTION (%d)%s\n' "$C_B" "$C_YEL" "${#WARNINGS[@]}" "$C_RST"
    local w
    for w in "${WARNINGS[@]}"; do printf '  %s•%s %s\n' "$C_YEL" "$C_RST" "$w"; done
  fi
}

render_json() {
  local td; td="$(mktemp -d)"
  printf '%s\n' "${AGENT_ROWS[@]+"${AGENT_ROWS[@]}"}" >"$td/agents"
  printf '%s\n' "${OTHER_ROWS[@]+"${OTHER_ROWS[@]}"}" >"$td/other"
  printf '%s\n' "${WARNINGS[@]+"${WARNINGS[@]}"}" >"$td/warn"
  {
    printf 'paused=%s\n' "$([ -f "$BASE/PAUSED" ] && echo true || echo false)"
    printf 'cap=%s\n' "$CAP"
    printf 'max_agents=%s\n' "$CFG_MAX_AGENTS"
    printf 'busy_window=%s\n' "$([ "$BUSY_WINDOW" = yes ] && echo true || echo false)"
    printf 'agents_running=%s\n' "${#AGENT_ROWS[@]}"
    printf 'idle_ticks=%s\n' "$IDLE_TICKS"
    printf 'slots_busy=%s\n' "${SLOTS_BUSY# }"
    printf 'slots_free=%s\n' "${SLOTS_FREE# }"
    printf 'claude_pct=%s\n' "$CL_PCT"
    printf 'claude_status=%s\n' "$CL_STATUS"
    printf 'claude_note=%s\n' "$CL_NOTE"
    printf 'ollama_pct=%s\n' "$OL_PCT"
    printf 'ollama_status=%s\n' "$OL_STATUS"
    printf 'ollama_note=%s\n' "$OL_NOTE"
    printf 'ready=%s\n' "$GH_READY"
    printf 'gh_ok=%s\n' "$([ "$GH_OK" = 1 ] && echo true || echo false)"
    printf 'working=%s\n' "$(printf '%s' "$GH_WORKING" | grep -c .)"
    printf 'blocked=%s\n' "$(printf '%s' "$GH_BLOCKED" | grep -c .)"
    printf 'needs_human=%s\n' "$(printf '%s' "$GH_HUMAN" | grep -c .)"
    printf 'open_prs=%s\n' "$(printf '%s' "$GH_PRS" | grep -c .)"
    printf 'last_tick_age_s=%s\n' "$LAST_TICK_AGE"
    printf 'window_hours=%s\n' "$WINDOW_HOURS"
    printf 'ticks=%s\n' "$(rollup_get TOTAL)"
    printf 'outcomes=%s\n' "$(rollup_get OUTCOMES)"
    printf 'modes=%s\n' "$(rollup_get MODES)"
    printf 'reasons=%s\n' "$(rollup_get REASONS)"
    printf 'worktrees=%s\n' "$WT_COUNT"
    printf 'disk_pct_used=%s\n' "$DISK_PCT"
    printf 'load=%s\n' "$LOADAVG"
  } >"$td/kv"
  python3 -c '
import json, sys

fields = ["pid", "tick_pid", "slot", "mode", "issue", "pr", "branch", "lane", "model",
          "route_reason", "elapsed_s", "timeout_s", "remaining_s", "risk", "attempts",
          "worktree", "transcript_age_s", "activity"]
td = sys.argv[1]

agents = []
for line in open(td + "/agents"):
    line = line.rstrip("\n")
    if not line.strip():
        continue
    parts = line.split("\x1f")
    parts += [""] * (len(fields) - len(parts))
    row = dict(zip(fields, parts))
    for k in ("pid", "tick_pid", "issue", "pr", "elapsed_s", "timeout_s", "remaining_s",
              "attempts", "transcript_age_s"):
        row[k] = int(row[k]) if str(row[k]).strip().isdigit() else None
    agents.append(row)

kv = {}
for line in open(td + "/kv"):
    if "=" in line:
        k, v = line.rstrip("\n").split("=", 1)
        if v in ("true", "false"):
            kv[k] = v == "true"
        elif v.lstrip("-").isdigit():
            kv[k] = int(v)
        else:
            kv[k] = v

def counter(s):
    out = {}
    for pair in (s or "").split(","):
        if ":" in pair:
            k, v = pair.rsplit(":", 1)
            out[k] = int(v) if v.isdigit() else v
    return out

other = []
for line in open(td + "/other"):
    line = line.rstrip("\n")
    if not line.strip():
        continue
    parts = (line.split("\x1f") + ["", "", ""])[:3]
    other.append({"pid": int(parts[0]) if parts[0].isdigit() else None,
                  "label": parts[1],
                  "elapsed_s": int(parts[2]) if parts[2].isdigit() else None})

warnings = [w for w in open(td + "/warn").read().splitlines() if w.strip()]
print(json.dumps({
    "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
    "pipeline": dict(
        {k: kv.get(k) for k in ("paused", "cap", "max_agents", "busy_window",
                                "agents_running", "idle_ticks", "last_tick_age_s")},
        # Always lists: a single busy slot would otherwise type as an int and a pair as a string,
        # so anything consuming this would need to handle both.
        slots_busy=str(kv.get("slots_busy", "")).split(),
        slots_free=str(kv.get("slots_free", "")).split(),
    ),
    "agents": agents,
    "other_claude": other,
    "lanes": {
        "claude": {"pct": kv.get("claude_pct"), "status": kv.get("claude_status"), "note": kv.get("claude_note")},
        "ollama": {"pct": kv.get("ollama_pct"), "status": kv.get("ollama_status"), "note": kv.get("ollama_note")},
    },
    "backlog": {k: kv.get(k) for k in ("gh_ok", "ready", "working", "blocked", "needs_human", "open_prs")},
    "recent": {"window_hours": kv.get("window_hours"), "ticks": kv.get("ticks"),
               "outcomes": counter(kv.get("outcomes")), "modes": counter(kv.get("modes")),
               "reasons": counter(kv.get("reasons"))},
    "host": {k: kv.get(k) for k in ("worktrees", "disk_pct_used", "load")},
    "warnings": warnings,
}, indent=2))
' "$td"
  rm -rf "$td"
}

run_once() {
  NOW="$(date +%s)"
  AGENT_ROWS=(); OTHER_ROWS=(); WARNINGS=(); IDLE_TICKS=0; SLOTS_BUSY=""; SLOTS_FREE=""
  GH_OK=0; GH_READY=0; GH_WORKING=""; GH_BLOCKED=""; GH_HUMAN=""; GH_PRS=""
  collect_pipeline_state
  collect_agents
  collect_other_claude
  collect_lanes
  collect_gh
  collect_rollup
  # The cap depends on the ready count. With --no-gh (or gh rate-limited) fall back to the count the
  # last tick recorded, so the cap line stays truthful instead of silently reading as 1.
  if [ "$GH_OK" != 1 ]; then
    GH_READY="$(tail -n 1 "$OUTCOMES" 2>/dev/null | python3 -c 'import json,sys
try: print(json.loads(sys.stdin.read() or "{}").get("ready_count", 0))
except Exception: print(0)' 2>/dev/null)"
    GH_READY="${GH_READY:-0}"
  fi
  compute_cap "${GH_READY:-0}"
  collect_slots
  collect_host
  if [ "$JSON" = 1 ]; then render_json; else render_text; fi
}

if [ "$WATCH" = 1 ]; then
  # trap so ^C leaves the terminal in a sane state rather than mid-redraw
  trap 'printf "\n"; exit 0' INT TERM
  while true; do
    clear 2>/dev/null
    run_once
    printf '\n%srefreshing every %ss — ^C to stop%s\n' "$C_DIM" "$WATCH_SECS" "$C_RST"
    sleep "$WATCH_SECS"
  done
else
  run_once
fi
