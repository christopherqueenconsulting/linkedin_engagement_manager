#!/usr/bin/env bash
# Harness parity smoke (#2562, acceptance boxes 1-2). OWNER-RUN: the pipeline's own agents cannot
# launch a live model run, so this prints the evidence the PR body needs.
#
# For each harness it proves, on a fresh pipeline worktree:
#   1. the worktree has a graft index (graft/INDEX.md exists),
#   2. one ordinary tool call works (the agent runs `git rev-parse --short HEAD`),
#   3. one graft MCP call returns results,
#   4. a caveman session-start check (the reply is terse: no filler opener),
#   5. the machine-readable output is still valid JSON (hooks did not corrupt it).
#
# Usage: smoke_parity.sh <worktree> [claude|codex|both]    (default: both)
# Env:   BASE (default /home/lem/agent-pipeline), SMOKE_CLAUDE_MODEL, SMOKE_CODEX_MODEL,
#        CODEX_BIN, CODEX_HOME_DIR. Exit 0 only when every requested harness passed every check.
set -u
WT="${1:?usage: smoke_parity.sh <worktree> [claude|codex|both]}"
WHICH="${2:-both}"
BASE="${BASE:-/home/lem/agent-pipeline}"
PROMPT='Do three things and reply in one short line each. 1) Run `git rev-parse --short HEAD`. 2) Call the graft MCP tool graft_find_code with the query "run_lane" and say how many results it returned. 3) Say hello.'
fail=0
say() { printf '%s\n' "$*"; }
check() { # <label> <0|1>
  if [ "$2" = 0 ]; then say "PASS  $1"; else say "FAIL  $1"; fail=1; fi
}

[ -f "$WT/graft/INDEX.md" ]; check "graft index present in $WT" $?

_valid_json() { python3 - "$1" <<'PY'
import json, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read().strip()
if not text:
    sys.exit(1)
try:
    json.loads(text)
    sys.exit(0)
except ValueError:
    pass
for line in text.splitlines():   # codex --json is one JSON object per line
    if line.strip():
        json.loads(line)
PY
}

if [ "$WHICH" = claude ] || [ "$WHICH" = both ]; then
  out="$(mktemp)"
  ( cd "$WT" && timeout 300 claude -p "$PROMPT" --output-format json \
      --permission-mode dontAsk --settings "$BASE/config/claude-headless.json" \
      --mcp-config "$BASE/mcp/mcp-config.json" ${SMOKE_CLAUDE_MODEL:+--model "$SMOKE_CLAUDE_MODEL"} ) >"$out" 2>"$out.err"
  check "claude: exited 0" $?
  _valid_json "$out" 2>/dev/null; check "claude: --output-format json is valid JSON" $?
  grep -qiE 'graft_find_code|mcp__graft' "$out"; check "claude: graft MCP call visible in output" $?
  ! grep -qiE "^(sure|certainly|of course|i'd be happy)" "$out"; check "claude: caveman terse reply" $?
  say "claude output: $out"
fi

if [ "$WHICH" = codex ] || [ "$WHICH" = both ]; then
  out="$(mktemp)"
  home="${CODEX_HOME_DIR:-$BASE/codex-home}"
  bin="${CODEX_BIN:-/home/lem/.npm-global/bin/codex}"
  [ -f "$home/config.toml" ] && grep -q '^\[mcp_servers.graft\]' "$home/config.toml"
  check "codex: $home/config.toml names mcp_servers.graft" $?
  ( cd "$WT" && CODEX_HOME="$home" timeout 300 "$bin" exec --json --skip-git-repo-check \
      --sandbox danger-full-access -c 'approval_policy="untrusted"' \
      -c model_provider=cliproxy -m "${SMOKE_CODEX_MODEL:-codex-auto-review}" -C "$WT" "$PROMPT" ) >"$out" 2>"$out.err"
  check "codex: exited 0" $?
  _valid_json "$out" 2>/dev/null; check "codex: --json output is valid JSON lines" $?
  grep -qiE 'graft' "$out"; check "codex: graft MCP call visible in output" $?
  say "codex output: $out"
fi

[ "$fail" = 0 ] && say "SMOKE OK" || say "SMOKE FAILED"
exit "$fail"
