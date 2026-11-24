#!/usr/bin/env bash
# Lane executor — the single place a `claude -p` run is launched, wrapped with capacity-aware lane
# env, PostHog lifecycle telemetry, outcome recording, and GitHub labeling. run_claude() in tick.sh
# and v2's agent_run.sh delegate here so every MODE (start/fix/review/selfreview/rebase/revise/
# depfix/depmajor/docfix/phasefix — all ten) gets identical
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
 "lane":os.environ.get("LANE",""),"provider":{"ollama":"ollama-cloud","codex":"codex-chatgpt"}.get(os.environ.get("LANE",""),"claude-subscription"),
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
# of quietly running every lane unrestricted. The refusal is EX_SETUP (73): an environment that is
# not ready, which the v2 daemon retries without parking — and callers check
# `permission_profile_ready` BEFORE charging a run budget, so a refusal never spends one.
#
# Neither setting is a boundary against the agent itself: both live in $BASE/config.env, which the
# runner's uid — and so code an allowed `pytest` runs — can write. The per-dispatch log lines below
# are how an `off` that nobody chose gets noticed.
PERMISSION_PROFILE_REFUSED=73

# _permission_profile_for <mode> -> prints the profile path that applies, or nothing when the mode
# runs on the old flag (`off`, or excluded by LEM_PERMISSION_PROFILE_MODES).
_permission_profile_for() {
  local modes="${LEM_PERMISSION_PROFILE_MODES:-}" profile="${LEM_PERMISSION_PROFILE:-}"
  modes="${modes// /}"
  [ "$profile" = "off" ] && return 0
  if [ -n "$modes" ]; then
    case ",${modes}," in *",${1:-},"*) ;; *) return 0 ;; esac
  fi
  printf '%s\n' "${profile:-$BASE/config/claude-headless.json}"
}

# permission_profile_ready [mode] -> 0 when run_lane would launch MODE, 1 when it would refuse.
# Quiet on purpose: callers log their own refusal next to the budget they did not charge.
permission_profile_ready() {
  local p; p="$(_permission_profile_for "${1:-${MODE:-}}")"
  [ -z "$p" ] || [ -f "$p" ]
}

# guard_ledger_charge_with_profile — v1's lanes charge `ledger_charge pr <n> <mode>` before
# calling run_claude, at six call sites. Rather than repeat the check at each, wrap ledger_charge
# once: for an agent MODE whose dispatch run_lane would refuse, charge nothing (prints 0). Every
# other ledger (merge, disarm) passes straight through.
guard_ledger_charge_with_profile() {
  command -v ledger_charge >/dev/null 2>&1 || return 0
  command -v _ledger_charge_unguarded >/dev/null 2>&1 && return 0   # already wrapped
  eval "_ledger_charge_unguarded() $(declare -f ledger_charge | tail -n +2)"
  ledger_charge() {
    case "${3:-}" in
      start|fix|review|selfreview|rebase|revise|depfix|depmajor|docfix|phasefix)
        if ! permission_profile_ready "$3"; then
          # stderr: callers capture or discard this function's stdout (the attempt number).
          log "LEDGER: not charging $1 #$2 $3 — run_lane would refuse it (permission profile missing)." >&2
          echo 0; return 0
        fi ;;
    esac
    _ledger_charge_unguarded "$@"
  }
}

_permission_args() {  # -> sets the caller's perm_args array + perm_profile; returns 73 on a bad profile
  local modes="${LEM_PERMISSION_PROFILE_MODES:-}" profile
  modes="${modes// /}"
  perm_profile=""
  perm_args=(--dangerously-skip-permissions)
  if [ "${LEM_PERMISSION_PROFILE:-}" = "off" ]; then
    log "run_lane: PERMISSION PROFILE OFF — LEM_PERMISSION_PROFILE=off, launching MODE=${MODE:-?} with --dangerously-skip-permissions (emergency opt-out; unset it to restore dontAsk)"
    return 0
  fi
  profile="$(_permission_profile_for "${MODE:-}")"
  if [ -z "$profile" ]; then
    log "run_lane: permission profile NOT applied to MODE=${MODE:-?} — LEM_PERMISSION_PROFILE_MODES='${modes}' restricts it; launching with --dangerously-skip-permissions. Clear LEM_PERMISSION_PROFILE_MODES to apply it to every MODE."
    return 0
  fi
  if [ ! -f "$profile" ]; then
    log "run_lane: REFUSING to dispatch — permission profile '${profile}' is not a file. Fix the path (or ship config/ with install.sh --sync); LEM_PERMISSION_PROFILE=off is the emergency opt-out. MODE=${MODE:-?}"
    return "$PERMISSION_PROFILE_REFUSED"
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

def _usage(obj):
    """Real token counts and the model that carried the run, from the CLI's result object.

    tokens_in counts every input token the run was billed or served for (fresh + cache write + cache
    read), because the router reads spend, not prompt size. The model is the one with the most tokens
    in `modelUsage`: a run can touch a small helper model, but the main model dominates.
    """
    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}

    def _n(value):
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    t_in = sum(_n(usage.get(k)) for k in (
        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    t_out = _n(usage.get("output_tokens"))
    model, best = "", -1
    by_model = obj.get("modelUsage") if isinstance(obj.get("modelUsage"), dict) else {}
    for name, stats in by_model.items():
        if not isinstance(stats, dict):
            continue
        total = sum(_n(stats.get(k)) for k in (
            "inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"))
        if total > best:
            model, best = str(name), total
    return t_in, t_out, model


if result is not None:
    t_in, t_out, used_model = _usage(result)
    with open(out_path + ".usage", "w", encoding="utf-8") as fh:
        fh.write(f"tokens_in={t_in}\ntokens_out={t_out}\nmodel_used={used_model}\n")
    tmp = out_path + ".text"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(kept) + "\n")
    os.replace(tmp, out_path)
PY
}

# ── Codex harness (#2561): the router's review lane ─────────────────────────────────────────────
# `codex exec` through CLIProxyAPI, review modes only. The sandbox is the exec-policy rules file
# (config/codex-review.rules) under `--sandbox danger-full-access`, because bubblewrap cannot create
# user namespaces on this host. Every failure that says "Codex is unavailable" (429, model_not_found,
# a timeout, a dead proxy, a bad key) pauses ONLY the Codex provider and falls to Sonnet on the
# Claude harness, then to Ollama tier 3. A missing binary is not one of them: it is an environment
# that is not ready, refused loudly with EX_SETUP and recorded as nothing.
CODEX_REFUSED=73
CODEX_PAUSED_FILE="${CODEX_PAUSED_FILE:-$BASE/CODEX_PAUSED_UNTIL}"
V2_DIR="${V2_DIR:-$BASE/v2}"

_codex_cli() { python3 "$V2_DIR/codex_lane_cli.py" "$@"; }

# _model_family_ok <claude|codex> <model> -> 0 when the pairing is allowed. Case-insensitive.
_model_family_ok() {
  local m="${2,,}"
  if [ "$1" = "codex" ]; then
    [ -n "$m" ] || return 1                       # never default a model onto Codex
    case "$m" in claude*|sonnet*|opus*|haiku*) return 1 ;; esac
    return 0
  fi
  case "$m" in gpt*|codex*|o[0-9]*) return 1 ;; esac
  return 0
}

# _lane_base_url <lane> -> the endpoint the launch line names for that lane.
_lane_base_url() {
  case "$1" in
    ollama) echo "$OLLAMA_LITELLM_URL" ;;
    codex)  echo "${CODEX_BASE_URL:-${CLIPROXY_BASE_URL:-}}" ;;
    *)      echo "${CLIPROXY_BASE_URL:-anthropic-default}" ;;
  esac
}

# _install_codex_config <codex-home> -> gives Codex the same MCP tools the Claude profile allows
# (graft, playwright, perplexity; #2562). The template carries a marker line, and only a file that
# is absent or already carries it is rewritten: a hand-edited config.toml is never overwritten.
# Fails open — a Codex review without graft is weaker, not wrong.
CODEX_CONFIG_MARKER="# lem-pipeline-managed: codex-pipeline.config.toml"
_install_codex_config() {
  local home="$1" tpl="$BASE/config/codex-pipeline.config.toml" dest="$1/config.toml"
  [ -f "$tpl" ] || return 0
  if [ -f "$dest" ] && ! grep -qF "$CODEX_CONFIG_MARKER" "$dest" 2>/dev/null; then
    log "codex config: $dest is not pipeline-managed; leaving it untouched."
    return 0
  fi
  cp "$tpl" "$dest" 2>/dev/null || log "codex config: could not install $dest; Codex runs without graft."
  return 0
}

# _codex_fallback <wt> <prompt> <hint> <class> -> pauses Codex and re-runs on the next provider.
_codex_fallback() {
  local wt="$1" prompt="$2" hint="$3" cls="$4" rc
  local pause_min="${CODEX_PAUSE_MINUTES:-${USAGE_PAUSE_MINUTES:-60}}"
  echo "$(( $(date +%s) + pause_min * 60 ))" > "$CODEX_PAUSED_FILE"
  log "codex run failed (${cls}) — pausing the Codex provider for ${pause_min}m; the Claude and Ollama lanes are untouched. Falling back to Claude Sonnet."
  local sonnet="${LEM_ROUTER_SONNET_MODEL:-claude-sonnet-5-5}" claude_until=0
  claude_until="$(cat "${CLAUDE_PAUSED_FILE:-/nonexistent}" 2>/dev/null || echo 0)"
  if [ "${claude_until:-0}" -gt "$(date +%s)" ] 2>/dev/null; then
    log "codex fallback: the Claude lane is paused — going straight to Ollama tier 3."
    LEM_LANE_OVERRIDE=ollama LEM_LANE_TIER=lem-agent-tier3 LEM_LANE_MODEL="" LEM_LANE_REASON="codex_fallback_${cls}" \
      LEM_LANE_MARKER="REVIEW QUALITY DEGRADED: Codex (${cls}) and Claude were unavailable; this review ran on Ollama tier 3." \
      run_lane "$wt" "$prompt" "$hint"
    return $?
  fi
  LEM_LANE_OVERRIDE=claude LEM_LANE_MODEL="$sonnet" LEM_LANE_REASON="codex_fallback_${cls}" \
    run_lane "$wt" "$prompt" "$hint"
  rc=$?
  if [ $rc -ne 0 ] && [ $rc -ne 124 ]; then
    log "codex fallback: Claude Sonnet failed (rc=$rc) — falling to Ollama tier 3."
    LEM_LANE_OVERRIDE=ollama LEM_LANE_TIER=lem-agent-tier3 LEM_LANE_MODEL="" LEM_LANE_REASON="codex_fallback_${cls}_then_claude" \
      LEM_LANE_MARKER="REVIEW QUALITY DEGRADED: Codex (${cls}) and Claude Sonnet failed; this review ran on Ollama tier 3." \
      run_lane "$wt" "$prompt" "$hint"
    rc=$?
  fi
  return $rc
}

# _run_codex_lane <wt> <prompt> <hint>  (LANE=codex, AGENT_MODEL set by dispatch_lane)
_run_codex_lane() {
  local wt="$1" prompt="$2" hint="$3" bin rules home out rc t0 ms cls tok_in=0 tok_out=0
  case "${MODE:-}" in review|selfreview) ;; *)
    log "run_lane: REFUSING the Codex harness for MODE=${MODE:-?} — it runs review modes only."
    return "$CODEX_REFUSED" ;;
  esac
  if ! bin="$(CODEX_BIN="${CODEX_BIN:-}" _codex_cli binary 2>/dev/null)"; then
    log "run_lane: REFUSING to dispatch to Codex — no executable codex at '${CODEX_BIN:-/home/lem/.npm-global/bin/codex}' (set CODEX_BIN to an absolute path; a unit's PATH does not include ~/.npm-global/bin). NOT a lane failure: no lane gauge was touched and no pause was set. MODE=${MODE:-?}"
    return "$CODEX_REFUSED"
  fi
  rules="$BASE/config/codex-review.rules"
  if [ ! -f "$rules" ]; then
    log "run_lane: REFUSING to dispatch to Codex — the exec-policy rules '${rules}' are missing; danger-full-access without them would be unrestricted. NOT a lane failure."
    return "$CODEX_REFUSED"
  fi
  if [ -z "$(_lane_base_url codex)" ] || [ -z "${CLIPROXY_AUTH_TOKEN:-}" ]; then
    log "run_lane: REFUSING to dispatch to Codex — CLIPROXY_BASE_URL / CLIPROXY_AUTH_TOKEN are not set. NOT a lane failure."
    return "$CODEX_REFUSED"
  fi
  home="${CODEX_HOME_DIR:-$BASE/codex-home}"
  mkdir -p "$home/rules" && cp "$rules" "$home/rules/default.rules" || {
    log "run_lane: REFUSING to dispatch to Codex — cannot install the rules under '$home'. NOT a lane failure."
    return "$CODEX_REFUSED"; }

  _install_codex_config "$home"
  _emit "issue_assigned"; _emit "ai_call_started"
  log "using lane=codex model=${AGENT_MODEL} base_url=$(_lane_base_url codex) (reason=${ROUTE_REASON:-})"
  out="$(mktemp "${LOGDIR:-/tmp}/run.XXXXXX")"; t0="$(date +%s)"
  local provider="model_providers.cliproxy={name=\"cliproxy\",base_url=\"$(_lane_base_url codex)\",env_key=\"CLIPROXY_AUTH_TOKEN\",wire_api=\"responses\"}"
  ( cd "$wt" && \
    CODEX_HOME="$home" GIT_EDITOR=true PATH="$(dirname "$bin"):/usr/local/bin:/usr/bin:/bin:$PATH" \
    timeout "${CODEX_TIMEOUT:-${CLAUDE_TIMEOUT:-45m}}" "$bin" exec --json --skip-git-repo-check \
      --sandbox danger-full-access -c 'approval_policy="untrusted"' \
      -c model_provider=cliproxy -c "$provider" -m "$AGENT_MODEL" -C "$wt" "$prompt" ) >"$out" 2>&1
  rc=$?
  ms=$(( ($(date +%s) - t0) * 1000 ))
  eval "$(_codex_cli usage "$out" 2>/dev/null)" 2>/dev/null
  tok_in="${tokens_in:-0}"; tok_out="${tokens_out:-0}"
  cat "$out" >> "${LOG:-/dev/null}"
  TICK_AGENT_RC="$rc"; export TICK_AGENT_RC
  case "$rc" in 126|127)
    log "codex run could not execute (rc=$rc). NOT recording a lane failure."
    rm -f "$out"; return "$CODEX_REFUSED" ;;
  esac
  cls="$(_codex_cli classify "$rc" "$out" 2>/dev/null)"
  _EMIT_EXTRA="{\"success\":$([ $rc -eq 0 ] && echo true || echo false),\"latency_ms\":$ms,\"tokens_in\":$tok_in,\"tokens_out\":$tok_out,\"total_tokens\":$(( tok_in + tok_out )),\"estimated_cost\":0,\"error_type\":\"${cls}\",\"fallback_from\":\"\",\"fallback_to\":\"\"}" \
    _emit "$([ $rc -eq 0 ] && echo ai_call_completed || echo ai_call_failed)"
  if [ -n "${LEMD_RUN_META:-}" ]; then
    printf 'lane=%s\nmodel=%s\nroute_reason=%s\ntokens_in=%s\ntokens_out=%s\n' \
      codex "$AGENT_MODEL" "${ROUTE_REASON:-}" "$tok_in" "$tok_out" >"$LEMD_RUN_META" 2>/dev/null || true
  fi
  rm -f "$out"
  if [ $rc -eq 0 ]; then _emit "issue_completed"; return 0; fi
  case "$cls" in
    rate_limit|model_not_found|timeout|proxy_down|bad_key)
      _EMIT_EXTRA="{\"fallback_from\":\"codex\",\"error_type\":\"${cls}\"}" _emit "fallback_triggered"
      _codex_fallback "$wt" "$prompt" "$hint" "$cls"; return $? ;;
  esac
  _EMIT_EXTRA="{\"error_type\":\"nonzero_exit\",\"error_message\":\"codex rc=$rc\"}" _emit "issue_failed"
  log "codex exited rc=$rc (${cls:-other}) — not a provider-availability failure; will retry next tick."
  return $rc
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
  _permission_args || return "$PERMISSION_PROFILE_REFUSED"

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
    local harness="claude-cli"
    [ "$LANE" = "codex" ] && harness="codex-exec"
    log "DRY_RUN: would run lane=$LANE harness=$harness model=${AGENT_MODEL:-default}" \
      "base_url=$(_lane_base_url "$LANE") tier=${AGENT_TIER:-} in $wt"
    return 0
  fi

  # One family per harness: a GPT model never goes through the Claude CLI and a Claude model never
  # through Codex. Checked on the model the dispatcher settled on, before anything launches.
  # Kept in shell (mirrors lemd.codex_lane.family_ok) so it needs no helper file on disk.
  if [ "$LANE" = "claude" ] || [ "$LANE" = "codex" ]; then
    if ! _model_family_ok "$LANE" "${AGENT_MODEL:-}"; then
      log "run_lane: REFUSING to dispatch — model '${AGENT_MODEL:-}' may not run on the $LANE harness" \
        "(GPT models run only on Codex, Claude models only on Claude)."
      return 1
    fi
  fi

  if [ "$LANE" = "codex" ]; then
    _run_codex_lane "$wt" "$prompt" "$hint"
    return $?
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
      ANTHROPIC_DEFAULT_SONNET_MODEL="${AGENT_TIER}" \
      ANTHROPIC_DEFAULT_OPUS_MODEL="${AGENT_TIER}" \
      ANTHROPIC_DEFAULT_HAIKU_MODEL="${AGENT_TIER}" \
      timeout "${CLAUDE_TIMEOUT:-45m}" claude -p "$prompt" \
        "${perm_args[@]}" --add-dir "$runbook_dir" "${model_arg[@]}" "${mcp_arg[@]}" ) >"$out" 2>&1
    rc=$?
  else
    ( cd "$wt" && \
      unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY 2>/dev/null || true
      # Claude lane via CLIProxyAPI (owner ruling 2026-11-12): no subscription token on this box.
      if [ -n "${CLIPROXY_BASE_URL:-}" ]; then export ANTHROPIC_BASE_URL="$CLIPROXY_BASE_URL" ANTHROPIC_AUTH_TOKEN="${CLIPROXY_AUTH_TOKEN:-}"; unset CLAUDE_CODE_OAUTH_TOKEN; fi
      GIT_EDITOR=true timeout "${CLAUDE_TIMEOUT:-45m}" claude -p "$prompt" \
        "${perm_args[@]}" --add-dir "$runbook_dir" "${model_arg[@]}" "${mcp_arg[@]}" ) >"$out" 2>&1
    rc=$?
  fi
  if [ -n "$perm_profile" ]; then
    _record_permission_run "$out" "$rc" "$perm_profile"
  fi
  # Real spend from the CLI's JSON result. Absent (an `off` profile run, a crash that printed no
  # result object) stays 0 — unmeasured, never a guess.
  local tok_in=0 tok_out=0 model_used=""
  if [ -f "$out.usage" ]; then
    tok_in="$(sed -n 's/^tokens_in=\([0-9]*\)$/\1/p' "$out.usage" | head -n1)"
    tok_out="$(sed -n 's/^tokens_out=\([0-9]*\)$/\1/p' "$out.usage" | head -n1)"
    model_used="$(sed -n 's/^model_used=\(.*\)$/\1/p' "$out.usage" | head -n1)"
    rm -f "$out.usage"
  fi
  tok_in="${tok_in:-0}"; tok_out="${tok_out:-0}"
  ms=$(( ($(date +%s) - t0) * 1000 ))
  cat "$out" >> "${LOG:-/dev/null}"
  # `[claude-code:unrecognized_model] {"model":"lem-agent-tier2",...}` is NOT a rejection. The CLI
  # prints it to stderr once per process for any model name outside its own catalogue, and then
  # sends the request anyway — every `lem-*` alias draws it on every Ollama-lane run, rc=0 runs
  # included. A model the endpoint refuses fails as `Invalid model name` instead, and the alias
  # probe in capacity.sh already holds the lane on that. Without this note the line, which lands
  # next to an unrelated timeout, reads as the cause (it did on 2026-11-12).
  if grep -qF '[claude-code:unrecognized_model]' "$out" 2>/dev/null; then
    log "note: [claude-code:unrecognized_model] is the CLI's non-fatal notice for a model name" \
      "it does not know (${AGENT_TIER:-${AGENT_MODEL:-default}}); the request still went to the" \
      "endpoint. Judge this run by its rc."
  fi

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

  _EMIT_EXTRA="{\"success\":$([ $rc -eq 0 ] && echo true || echo false),\"latency_ms\":$ms,\"retry_count\":0,\"tokens_in\":$tok_in,\"tokens_out\":$tok_out,\"total_tokens\":$(( tok_in + tok_out )),\"estimated_cost\":0,\"error_type\":\"$([ $rc -ne 0 ] && echo nonzero_exit)\",\"fallback_from\":\"${FALLBACK_FROM}\",\"fallback_to\":\"${FALLBACK_TO}\"}" \
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
    # `model` is what the CLI reported it ran, falling back to the routed name when unmeasured.
    printf 'lane=%s\nmodel=%s\nroute_reason=%s\ntokens_in=%s\ntokens_out=%s\n' \
      "$LANE" "${model_used:-${AGENT_TIER:-${AGENT_MODEL:-}}}" "${ROUTE_REASON:-}" \
      "$tok_in" "$tok_out" >"$LEMD_RUN_META" 2>/dev/null || true
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
    # 124 is coreutils `timeout`: the run spent its whole CLAUDE_TIMEOUT budget. Say so, rather
    # than a generic triple that invites hunting for a model or endpoint fault that is not there.
    if [ $rc -eq 124 ]; then
      log "$LANE claude exited rc=124 — ran out its ${CLAUDE_TIMEOUT:-45m} budget (CLAUDE_TIMEOUT)," \
        "not a model error — will retry next tick."
    else
      log "$LANE claude exited rc=$rc (timeout/interrupt/limit) — will retry next tick."
    fi
  fi
  rm -f "$out"
  return $rc
}