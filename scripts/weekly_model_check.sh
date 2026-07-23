#!/usr/bin/env bash
# Weekly LiteLLM model-health orchestrator (host cron, runs as `lem`).
#
# Flow: plan (scripts/model_health_check.py against the LIVE box config) -> for retirements with a
# verified replacement, back up + apply the swap on the box, restart litellm, smoke-test every tier,
# and ROLL BACK on any failure; on success open a PR so the change lands in git and alert. When no
# swap is needed but a tier is 410-ing (a stale litellm that wasn't restarted after a deploy), just
# restart + re-verify. Manual-only retirements (REMOVE / unknown / no working replacement) alert.
#
# Safe by construction: a replacement is provider-verified before use, changes are smoke-tested
# before they're kept, and deploy.sh resets .litellm before its checkout so an on-box hotfix can
# never block a release.
set -uo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/home/lem/.local/bin"

REPO="/home/lem/linkedin_engagement_manager"
BOX_CFG="/opt/lem/.litellm/config.yaml"
MAP="$REPO/.litellm/model_upgrades.yaml"
DIR="/home/lem/model-check"
LOG="$DIR/model_check.log"
COMPOSE="sudo -n docker compose -f /opt/lem/docker-compose.yml -f /opt/lem/docker-compose.prod.yml"
TIERS="lem-simple lem-medium lem-complex lem-router"
mkdir -p "$DIR"

log(){ echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG" >&2; }

alert(){  # log + best-effort email to the admin; never fails the run
  log "ALERT: $1"
  sudo -n docker exec -i web_app python - "$1" <<'PY' >>"$LOG" 2>&1 || true
import os, sys
msg = sys.argv[1]
try:
    from cqc_lem.utilities.email import _dispatch_email
    to = os.environ.get("ADMIN_EMAIL") or os.environ.get("LINKEDIN_EMAIL") or "christopher.queen@gmail.com"
    html = "<pre>" + msg.replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
    _dispatch_email(to, "LEM weekly model-health check", html, text_content=msg, high_priority=True)
    print("alert emailed to", to)
except Exception as e:
    print("alert email failed (logged only):", e)
PY
}

restart_litellm(){ log "restarting litellm"; $COMPOSE restart litellm >>"$LOG" 2>&1; sleep 8; }

tier_ok(){  # $1=tier -> 0 if a completion succeeds within 3 tries
  local t="$1" i
  for i in 1 2 3; do
    if sudo -n docker exec web_app python -c "
from cqc_lem.utilities.ai.client import client
client.chat.completions.create(model='$t', messages=[{'role':'user','content':'ok'}], max_tokens=3)
" >/dev/null 2>&1; then return 0; fi
    sleep 4
  done
  return 1
}

smoke(){  # 0 iff every tier answers
  local bad=0 t
  for t in $TIERS; do
    if tier_ok "$t"; then log "smoke $t: OK"; else log "smoke $t: FAIL"; bad=1; fi
  done
  return $bad
}

open_pr(){  # mirror the same swap into the repo via an EPHEMERAL worktree — never touches the
            # shared dev checkout's branch/working tree (a human may be working there).
  local wt; wt="$(mktemp -d /tmp/model-upgrade-wt.XXXXXX)"
  ( cd "$REPO" && git fetch -q origin main \
      && git worktree add -q -B auto/model-upgrade "$wt" origin/main ) || { log "worktree add failed"; return 1; }
  (
    cd "$wt" || exit 1
    poetry run python "$REPO/scripts/model_health_check.py" --apply "$wt/.litellm/config.yaml" \
        --config "$wt/.litellm/config.yaml" --map "$wt/.litellm/model_upgrades.yaml" >>"$LOG" 2>&1
    git add .litellm/config.yaml
    git commit -q -m "fix(litellm): auto-swap retired Ollama model(s) [weekly model-health check]" \
        -m "Opened by scripts/weekly_model_check.sh after verifying the replacement against the provider and smoke-testing every tier on the live box." >>"$LOG" 2>&1 || { log "no repo diff to PR"; exit 0; }
    git push -q -u origin auto/model-upgrade >>"$LOG" 2>&1
    gh pr create --base main --head auto/model-upgrade \
       --title "fix(litellm): auto-swap retired Ollama model(s)" \
       --body "Automated by the weekly model-health check. The replacement was verified against Ollama Cloud and every tier smoke-tested on the box before this PR." >>"$LOG" 2>&1 \
       && log "PR opened" || log "PR create skipped (maybe one already open)"
  )
  ( cd "$REPO" && git worktree remove --force "$wt" 2>/dev/null ) || true
}

log "=== weekly model-health check start ==="
# Provider creds for the planner's direct probes.
set -a; source <(sudo -n grep -E "^(OLLAMA_CLOUD_URL|OLLAMA_CLOUD_API_KEY)=" /opt/lem/.env); set +a

cd "$REPO"
PLAN="$(poetry run python scripts/model_health_check.py --plan-json --config "$BOX_CFG" --map "$MAP" 2>>"$LOG")"
[ -z "$PLAN" ] && { log "planner produced no output — aborting"; exit 1; }
NSWAP=$(printf '%s' "$PLAN" | python3 -c "import sys,json;print(len(json.load(sys.stdin)['swaps']))" 2>/dev/null || echo 0)
NALERT=$(printf '%s' "$PLAN" | python3 -c "import sys,json;print(len(json.load(sys.stdin)['alerts']))" 2>/dev/null || echo 0)
log "plan: swaps=$NSWAP alerts=$NALERT"

if [ "$NALERT" -gt 0 ]; then
  MSG=$(printf '%s' "$PLAN" | python3 -c "import sys,json;print(chr(10).join(f\"{a['group']}: {a.get('model','')} - {a['reason']}\" for a in json.load(sys.stdin)['alerts']))")
  alert "Model retirements needing manual action:"$'\n'"$MSG"
fi

if [ "$NSWAP" -gt 0 ]; then
  BK="$BOX_CFG.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  sudo -n cp -a "$BOX_CFG" "$BK"; log "backed up box config -> $BK"
  TMP=$(mktemp)
  poetry run python scripts/model_health_check.py --apply "$TMP" --config "$BOX_CFG" --map "$MAP" >>"$LOG" 2>&1
  sudo -n cp "$TMP" "$BOX_CFG"; rm -f "$TMP"
  restart_litellm
  if smoke; then
    log "swaps verified — keeping"
    open_pr
    alert "Applied + verified $NSWAP model swap(s); PR opened to make it durable."
  else
    log "smoke FAILED after swaps — rolling back to $BK"
    sudo -n cp "$BK" "$BOX_CFG"; restart_litellm
    alert "Model swap smoke-test FAILED — rolled back. Manual review needed."
    exit 1
  fi
else
  if smoke; then
    log "all tiers healthy; nothing to do"
  else
    log "no swaps but a tier failed — restarting litellm (likely stale config after a deploy)"
    restart_litellm
    if smoke; then log "recovered after restart"; else alert "Tiers still failing after litellm restart — manual review."; fi
  fi
fi
log "=== weekly model-health check done ==="
