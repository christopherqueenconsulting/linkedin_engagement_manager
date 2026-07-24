#!/usr/bin/env bash
# Weekly LinkedIn API-version orchestrator (host cron, runs as `lem`).
#
# LinkedIn retires versioned-API versions on a rolling window (~7 months live as of 2026-07).
# A retired LI_API_VERSION makes every /rest/* call answer 426 NONEXISTENT_VERSION, which
# silently demotes native document posts to the legacy path. This keeps the pin alive.
#
# Flow: plan (scripts/linkedin_version_check.py, probing LinkedIn from inside web_app) -> if the
# pin is retired or near the retirement edge, GATE on the unit suite, back up + bump
# /opt/lem/.env, recreate the app services, smoke-test the deployed version against the live API,
# and ROLL BACK on any failure; on success open a PR so the repo defaults follow, and alert.
#
# Safe by construction: the target version is probe-verified before use, the unit suite must pass
# before prod is touched, the change is smoke-tested against LinkedIn after the restart, and a
# timestamped .env backup is restored automatically if anything fails.
set -uo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/home/lem/.local/bin"

# Self-locate the repo root so this can run from a dedicated cron clone, isolated from any
# interactive dev checkout (overridable via REPO=... for tests).
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="/opt/lem/.env"
DIR="/home/lem/li-version-check"
LOG="$DIR/li_version_check.log"
COMPOSE="sudo -n docker compose -f /opt/lem/docker-compose.yml -f /opt/lem/docker-compose.prod.yml"
APP_SERVICES="web_app celery_worker celery_worker_selenium celery_worker_selenium_outreach celery_worker_selenium_content celery_beat flower"
MIN_HEADROOM="${MIN_HEADROOM:-2}"
# DRY_RUN=1 plans and runs the unit gate, but never writes .env, recreates containers, opens a
# PR or emails — so the bump path can be rehearsed against prod safely.
DRY_RUN="${DRY_RUN:-0}"
mkdir -p "$DIR"

# Python with pytest (unit gate). Prefer the stable dev-checkout venv; fall back to poetry.
_DEV_VENV_PY="/home/lem/linkedin_engagement_manager/.venv/bin/python"
if [ -x "$_DEV_VENV_PY" ]; then PY=("$_DEV_VENV_PY"); else PY=(poetry run python); fi

log(){ echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG" >&2; }

alert(){  # log + best-effort email to the admin; never fails the run
  log "ALERT: $1"
  [ "$DRY_RUN" = "1" ] && { log "DRY_RUN: skipping alert email"; return 0; }
  sudo -n docker exec -i web_app python - "$1" <<'PY' >>"$LOG" 2>&1 || true
import os, sys
msg = sys.argv[1]
try:
    from cqc_lem.utilities.email import _dispatch_email
    to = os.environ.get("ADMIN_EMAIL") or os.environ.get("LINKEDIN_EMAIL") or "christopher.queen@gmail.com"
    html = "<pre>" + msg.replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
    _dispatch_email(to, "LEM weekly LinkedIn API-version check", html, text_content=msg, high_priority=True)
    print("alert emailed to", to)
except Exception as e:
    print("alert email failed (logged only):", e)
PY
}

plan_json(){  # probe LinkedIn from inside web_app (it has the DB token); $1=current version
  sudo -n docker exec -i web_app python - --plan-json --current "$1" --min-headroom "$MIN_HEADROOM" \
    < "$REPO/scripts/linkedin_version_check.py" 2>>"$LOG"
}

jget(){ printf '%s' "$1" | python3 -c "import sys,json;print(json.load(sys.stdin).get('$2',''))" 2>/dev/null; }

recreate_apps(){  # `restart` reuses the old env — the containers must be RECREATED to pick up .env
  log "recreating app services"
  # shellcheck disable=SC2086
  $COMPOSE up -d --no-deps --pull never $APP_SERVICES >>"$LOG" 2>&1
}

wait_healthy(){  # 0 once no app container is still starting (bounded wait)
  local i
  for i in $(seq 1 60); do
    if ! sudo -n docker ps --format '{{.Status}}' | grep -q 'health: starting'; then return 0; fi
    sleep 5
  done
  return 1
}

smoke(){  # $1=expected version — the DEPLOYED containers must carry it AND LinkedIn must accept it
  local want="$1" got
  got="$(sudo -n docker exec web_app printenv LI_API_VERSION 2>/dev/null)"
  if [ "$got" != "$want" ]; then log "smoke: web_app has LI_API_VERSION=$got, expected $want"; return 1; fi
  got="$(sudo -n docker exec celery_worker printenv LI_API_VERSION 2>/dev/null)"
  if [ "$got" != "$want" ]; then log "smoke: celery_worker has LI_API_VERSION=$got, expected $want"; return 1; fi
  # A live versioned call with the deployed pin — this is the failure the whole job exists to catch.
  if ! sudo -n docker exec -i web_app python - <<'PY' >>"$LOG" 2>&1
import sys, requests
from cqc_lem.utilities.db import get_user_access_token
from cqc_lem.utilities.env_constants import LI_API_VERSION
r = requests.get("https://api.linkedin.com/rest/me", timeout=20, headers={
    "Authorization": f"Bearer {get_user_access_token(1)}", "LinkedIn-Version": LI_API_VERSION})
print(f"probe {LI_API_VERSION} -> {r.status_code}")
sys.exit(1 if r.status_code == 426 else 0)
PY
  then log "smoke: LinkedIn rejected the deployed version"; return 1; fi
  log "smoke: OK ($want live in web_app + celery_worker)"
  return 0
}

open_pr(){  # mirror the bump into the repo via an EPHEMERAL worktree — never touches the shared
            # dev checkout's branch/working tree (a human may be working there). $1=version
  local version="$1" wt; wt="$(mktemp -d /tmp/li-version-wt.XXXXXX)"
  ( cd "$REPO" && git fetch -q origin main \
      && git worktree add -q -B auto/li-api-version "$wt" origin/main ) || { log "worktree add failed"; return 1; }
  (
    cd "$wt" || exit 1
    "${PY[@]}" "$REPO/scripts/linkedin_version_check.py" --rewrite-repo-defaults "$wt" \
        --version "$version" >>"$LOG" 2>&1
    git add .env.example src/cqc_lem/utilities/env_constants.py
    git commit -q -m "fix(linkedin): bump LI_API_VERSION to $version [weekly version check]" \
        -m "Opened by scripts/weekly_linkedin_version_check.sh after probing LinkedIn for the live version window, passing the unit suite, and smoke-testing the bump on the box." >>"$LOG" 2>&1 \
        || { log "no repo diff to PR"; exit 0; }
    git push -q -u origin auto/li-api-version >>"$LOG" 2>&1
    gh pr create --base main --head auto/li-api-version \
       --title "fix(linkedin): bump LI_API_VERSION to $version" \
       --body "Automated by the weekly LinkedIn API-version check. \`$version\` was probe-verified against the live API, the unit suite passed, and the bump was smoke-tested on the box before this PR. Production is already on it — this keeps the repo defaults in step." >>"$LOG" 2>&1 \
       && log "PR opened" || log "PR create skipped (maybe one already open)"
  )
  ( cd "$REPO" && git worktree remove --force "$wt" 2>/dev/null ) || true
}

log "=== weekly LinkedIn API-version check start ==="
CURRENT="$(sudo -n grep -E "^LI_API_VERSION=" "$ENV_FILE" | head -1 | cut -d= -f2)"
log "configured LI_API_VERSION=${CURRENT:-<unset>}"

PLAN="$(plan_json "$CURRENT")"
[ -z "$PLAN" ] && { log "planner produced no output — aborting"; alert "LinkedIn version check could not run (no planner output)."; exit 1; }
ACTION="$(jget "$PLAN" action)"
TARGET="$(jget "$PLAN" target)"
REASON="$(jget "$PLAN" reason)"
log "plan: action=$ACTION target=$TARGET ($REASON)"

case "$ACTION" in
  none)
    log "nothing to do"; log "=== done ==="; exit 0 ;;
  error)
    alert "LinkedIn API-version check FAILED: $REASON"; exit 1 ;;
  bump|urgent-bump) ;;
  *)
    alert "LinkedIn API-version check: unknown action '$ACTION'"; exit 1 ;;
esac

# --- gate on the unit suite BEFORE prod is touched ------------------------------------------
log "running unit suite as the gate"
if ! ( cd "$REPO" && PYTHONPATH="$REPO/src" "${PY[@]}" -m pytest tests/unit -q -p no:cacheprovider \
        --no-header >>"$LOG" 2>&1 ); then
  alert "LinkedIn API-version bump to $TARGET ABORTED — unit suite failed. Prod untouched (still $CURRENT)."
  exit 1
fi
log "unit suite passed"

# --- apply to prod, verify, roll back on failure ---------------------------------------------
if [ "$DRY_RUN" = "1" ]; then
  log "DRY_RUN: would back up $ENV_FILE, set LI_API_VERSION=$TARGET, recreate [$APP_SERVICES],"
  log "DRY_RUN: smoke-test $TARGET against LinkedIn, open a PR, and alert. Nothing changed."
  log "=== dry run done ==="; exit 0
fi

BK="$ENV_FILE.bak.$(date -u +%Y%m%dT%H%M%SZ)"
sudo -n cp -a "$ENV_FILE" "$BK"; log "backed up env -> $BK"
if sudo -n grep -qE "^LI_API_VERSION=" "$ENV_FILE"; then
  sudo -n sed -i "s/^LI_API_VERSION=.*$/LI_API_VERSION=$TARGET/" "$ENV_FILE"
else  # key absent entirely — append it, or the bump would be a silent no-op
  echo "LI_API_VERSION=$TARGET" | sudo -n tee -a "$ENV_FILE" >/dev/null
fi
log "set LI_API_VERSION=$TARGET in $ENV_FILE"

recreate_apps
wait_healthy || log "warning: containers still reporting 'health: starting' after the wait"

if smoke "$TARGET"; then
  log "bump verified — keeping"
  open_pr "$TARGET"
  alert "LinkedIn API version bumped ${CURRENT:-<unset>} -> $TARGET and verified live. Reason: $REASON. PR opened to sync the repo defaults."
else
  log "smoke FAILED — rolling back to $BK"
  sudo -n cp -a "$BK" "$ENV_FILE"
  recreate_apps
  wait_healthy || true
  alert "LinkedIn API-version bump to $TARGET FAILED smoke-test — rolled back to ${CURRENT:-<unset>}. Manual review needed."
  exit 1
fi
log "=== weekly LinkedIn API-version check done ==="
