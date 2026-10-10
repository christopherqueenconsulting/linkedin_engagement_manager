#!/usr/bin/env bash
# Lane executor — the single place a `claude -p` run is launched, wrapped with capacity-aware lane
# env, PostHog lifecycle telemetry, outcome recording, and GitHub labeling. run_claude() in tick.sh
# delegates here so every MODE (depfix/revise/rebase/fix/review/selfreview/start) gets identical
# routing + observability without per-call-site edits. Every MODE runs headless under the `dontAsk`
# permission profile ($BASE/config/claude-headless.json) — anything the profile does not allow is
# denied, never prompted. LEM_PERMISSION_PROFILE=off is the emergency opt-out back to the old
# --dangerously-skip-permissions flag; see _permission_args below.
#
# Env read (set by tick.sh before each call): MODE, ISSUE, PR, BRANCH, WORKTREE, RISK, SLOT,
#   WORKER_ID, EXECUTION_ID, _TICK_LOG, LOG, LOGDIR, CLAUDE_TIMEOUT, DRY_RUN,
#   LEM_PERMISSION_PROFILE, LEM_PERMISSION_PROFILE_MODES.
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

# The pipeline's OWN Claude credential — a long-lived `claude setup-token` value in secrets.env — so
# an interactive `/login` on any machine can no longer rotate the credential this runner
# authenticates with. v2 inherits it from systemd (`EnvironmentFile` on lem-agentd.service); the v1
# failsafe runs from cron, which has no systemd env, so this line is the only thing that reaches a
# `claude -p` child on that path. Exported ONLY when non-empty: an empty value is not "unset" to the
# CLI, it is a credential that fails, and it would shadow ~/.claude/.credentials.json on a box that
# has not installed a token yet.
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  export CLAUDE_CODE_OAUTH_TOKEN
fi

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

# The permission profile — the DEFAULT for every MODE. Each agent launches with
# `--permission-mode dontAsk --settings <profile> --output-format json`: anything the profile does
# not allow is DENIED (there is no human to ask), and every run is logged to denials.jsonl.
#
#   LEM_PERMISSION_PROFILE        unset/empty -> $BASE/config/claude-headless.json (the default)
#                                 <path>      -> that settings file instead
#                                 off         -> EMERGENCY OPT-OUT: the old
#                                                --dangerously-skip-permissions argv, with a loud
#                                                log line on every dispatch so it cannot be
#                                                forgotten in place
#   LEM_PERMISSION_PROFILE_MODES  unset/empty -> every MODE gets the profile
#                                 `selfreview,review` -> ONLY those MODEs get it; every other MODE
#                                 falls back to the old flag, logged per dispatch. This is a
#                                 RESTRICTION and applies only when explicitly set: a box-local
#                                 value left over from the one-lane shadow keeps limiting the
#                                 profile until the operator clears it.
#
# A profile path that is not a file REFUSES the dispatch rather than falling back to the old argv —
# including the default path, so a box whose installer never shipped config/ stops loudly instead
# of quietly running every lane unrestricted.
_permission_args() {  # -> sets the caller's perm_args array + perm_profile; returns 1 on a bad profile
  local modes="${LEM_PERMISSION_PROFILE_MODES:-}" profile="${LEM_PERMISSION_PROFILE:-}"
  modes="${modes// /}"
  perm_profile=""
  perm_args=(--dangerously-skip-permissions)
  if [ "$profile" = "off" ]; then
    log "run_lane: PERMISSION PROFILE OFF — LEM_PERMISSION_PROFILE=off, launching MODE=${MODE:-?} with --dangerously-skip-permissions (emergency opt-out; unset it to restore dontAsk)"
    return 0
  fi
  [ -z "$profile" ] && profile="$BASE/config/claude-headless.json"
  if [ -n "$modes" ]; then
    case ",${modes}," in
      *",${MODE:-},"*) ;;
      *)
        log "run_lane: permission profile NOT applied to MODE=${MODE:-?} — LEM_PERMISSION_PROFILE_MODES='${modes}' restricts it; launching with --dangerously-skip-permissions. Clear LEM_PERMISSION_PROFILE_MODES to apply it to every MODE."
        return 0 ;;
    esac
  fi
  if [ ! -f "$profile" ]; then
    log "run_lane: REFUSING to dispatch — permission profile '${profile}' is not a file. Fix the path (or ship config/ with install.sh --sync); LEM_PERMISSION_PROFILE=off is the emergency opt-out. MODE=${MODE:-?}"
    return 1
  fi
  perm_profile="$profile"
  perm_args=(--permission-mode dontAsk --settings "$perm_profile" --output-format json)
}

# Profile runs print ONE JSON result object on stdout instead of the agent's text. Everything
# downstream of the run (the $LOG append, the usage-limit grep) reads text, so put the `.result` text
# back into the same output file, keep any non-JSON lines (stderr) around it, and append one line per
# run to denials.jsonl. Unparseable output is left exactly as it was: a crash dump is more useful in
# $LOG than nothing, and the denials line records `parsed: false` so the gap is visible.
_record_permission_run() {  # $1=output file (rewritten in place)  $2=agent rc  $3=profile path
  local denials_log="${LOGDIR:-$BASE/logs}/denials.jsonl"
  python3 - "$1" "$denials_log" "$2" "${LANE:-}" "${MODE:-}" "${ISSUE:-}" "${PR:-}" \
    "${EXECUTION_ID:-}" "${3:-}" <<'PY' 2>/dev/null || true
import json, os, sys, time

out_path, log_path, rc, lane, mode, issue, pr, execution_id, profile = sys.argv[1:10]
try:
    with open(out_path, encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
except OSError:
    raw = ""

result, kept = None, []
decoder = json.JSONDecoder()
for line in raw.splitlines():
    s = line.strip()
    if result is None and s.startswith("{"):
        # raw_decode, not loads: stdout and stderr share the file, so a stderr line can land on the
        # same line as the JSON object when stdout ends without a newline.
        try:
            obj, end = decoder.raw_decode(s)
        except ValueError:
            obj, end = None, 0
        if isinstance(obj, dict) and obj.get("type") == "result":
            result = obj
            # No text to hand back (an error subtype) -> keep the raw object, so a usage-limit or
            # max-turns message still reaches $LOG and the usage-limit grep.
            kept.append(obj["result"] if isinstance(obj.get("result"), str) else s[:end])
            if s[end:].strip():
                kept.append(s[end:].strip())
            continue
    kept.append(line)


def _clip(value):
    """Bound one denial's input so a pasted file cannot blow up a log line."""
    text = json.dumps(value, default=str)
    return value if len(text) <= 2000 else text[:2000] + "...[truncated]"


denials = (result or {}).get("permission_denials") or []
entry = {
    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "lane": lane,
    "mode": mode,
    "issue": issue,
    "pr": pr,
    "execution_id": execution_id,
    "profile": profile,
    "rc": int(rc) if rc.lstrip("-").isdigit() else rc,
    "parsed": result is not None,
    "denial_count": len(denials) if result is not None else None,
    "denials": [
        {"tool_name": d.get("tool_name"), "tool_input": _clip(d.get("tool_input"))}
        for d in denials if isinstance(d, dict)
    ],
}
os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
with open(log_path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(entry) + "\n")

if result is not None:
    tmp = out_path + ".text"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(kept) + "\n")
    os.replace(tmp, out_path)
PY
}

run_lane() {  # $1=worktree  $2=prompt  $3=claude_model_hint
  local wt="$1" prompt="$2" hint="${3:-}" out rc t0 ms
  local perm_args=() perm_profile=""

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
  _permission_args || return 1

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
  # Be precise about what this buys: `--add-dir` scopes the FILE tools, but the Bash tool runs as
  # the same uid as this runner — under the dontAsk profile a command it allows (pytest, say) can
  # still read anything this uid can read, and under the `off` opt-out every command can. So this
  # is defense in depth, NOT the control.
  # The control is custody: the App key is root-owned in /etc/lem and minted into a ~1h token by
  # lem-gh-token.timer, so the worst an agent can reach is a credential that expires within the
  # hour and carries authority the pipeline already has — instead of a key that never expires.
  local runbook_dir; runbook_dir="$(dirname "${RUNBOOK:-$BASE/RUNBOOK.md}")"
  # GIT_EDITOR=true on both launches: a headless run has no editor, so `git rebase --continue`
  # (MODE=rebase) would otherwise wait on one until the lane timeout, and under the dontAsk profile
  # the agent cannot prefix the assignment itself — an allow rule does not match past it.
  if [ "$LANE" = "ollama" ]; then
    ( cd "$wt" && \
      GIT_EDITOR=true \
      ANTHROPIC_BASE_URL="$OLLAMA_LITELLM_URL" \
      ANTHROPIC_AUTH_TOKEN="$LITELLM_MASTER_KEY" \
      ANTHROPIC_API_KEY="$LITELLM_MASTER_KEY" \
      timeout "${CLAUDE_TIMEOUT:-45m}" claude -p "$prompt" \
        "${perm_args[@]}" --add-dir "$runbook_dir" "${model_arg[@]}" "${mcp_arg[@]}" ) >"$out" 2>&1
    rc=$?
  else
    ( cd "$wt" && \
      unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY 2>/dev/null || true
      GIT_EDITOR=true timeout "${CLAUDE_TIMEOUT:-45m}" claude -p "$prompt" \
        "${perm_args[@]}" --add-dir "$runbook_dir" "${model_arg[@]}" "${mcp_arg[@]}" ) >"$out" 2>&1
    rc=$?
  fi
  if [ -n "$perm_profile" ]; then
    _record_permission_run "$out" "$rc" "$perm_profile"
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
  # A failure that never REACHED a model is not evidence about the lane's health, and recording it
  # as one is self-harming: capacity.sh marks a lane constrained after 3 consecutive failures, so
  # three rc=127s in a row take the Claude subscription out of rotation for 30 minutes and send
  # every dispatch to Ollama. That happened for real on 2026-08-10 — the v2 daemon had no
  # `~/.local/bin` on its PATH, four runs exited 127, and the next 30 dispatches all logged
  # `reason=fallback` while the Claude lane was perfectly healthy and the owner's Ollama quota
  # burned. The lane gauge must only be fed by runs that actually talked to a model.
  #
  # 126/127 are the unambiguous shell answers for "could not execute" — not-executable and
  # not-found. Anything else (including a timeout, which DID reach the model) still counts.
  if [ $rc -eq 127 ] || [ $rc -eq 126 ]; then
    log "$LANE run could not execute (rc=$rc — interpreter missing or not executable). NOT recording a lane failure: this says nothing about $LANE's capacity."
  else
    record_lane_outcome "$LANE" $([ $rc -eq 0 ] && echo 1 || echo 0) "$ul"
  fi

  _EMIT_EXTRA="{\"success\":$([ $rc -eq 0 ] && echo true || echo false),\"latency_ms\":$ms,\"retry_count\":0,\"tokens_in\":0,\"tokens_out\":0,\"total_tokens\":0,\"estimated_cost\":0,\"error_type\":\"$([ $rc -ne 0 ] && echo nonzero_exit)\",\"fallback_from\":\"${FALLBACK_FROM}\",\"fallback_to\":\"${FALLBACK_TO}\"}" \
    _emit "$([ $rc -eq 0 ] && echo ai_call_completed || echo ai_call_failed)"

  # Report the lane back to whoever spawned us. The v2 daemon opens the `runs` row BEFORE the lane
  # exists — routing is decided here, in the child — so `runs.lane`/`model`/`route_reason` were NULL
  # on every row ever written (59 of 59 on cutover day). That is not a cosmetic gap: it makes
  # "which lane is failing?" unanswerable from the queue, and that is the exact question #1311 says
  # to settle before the start-lane throttle is lifted.
  #
  # A file rather than an exit code or a log grep: the daemon already knows this path (it passes
  # it), a write is atomic enough at this size, and a child that dies before writing simply leaves
  # the columns NULL — the status quo, never a wrong attribution.
  if [ -n "${LEMD_RUN_META:-}" ]; then
    printf 'lane=%s\nmodel=%s\nroute_reason=%s\n' \
      "$LANE" "${AGENT_TIER:-${AGENT_MODEL:-}}" "${ROUTE_REASON:-}" >"$LEMD_RUN_META" 2>/dev/null || true
  fi

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