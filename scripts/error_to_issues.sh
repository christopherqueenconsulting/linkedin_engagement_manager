#!/usr/bin/env bash
# Daily error->issues cron (host cron, runs as `lem`) — issue #648.
#
# Replaces the old ~/error-to-issues/scan.sh, which grepped PostHog LOGS for ERROR/FATAL bodies and
# hand-rolled dedup off a sha1 of the message string. This one reads PostHog Error Tracking ISSUES,
# where grouping is already done by fingerprint, and files ONE GitHub issue per PostHog issue id.
#
# Cron (as `lem`):
#   30 8 * * * /home/lem/linkedin_engagement_manager/scripts/error_to_issues.sh
set -uo pipefail
export PATH="/home/lem/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export HOME="${HOME:-/home/lem}"

# Self-locate the repo root so this can run from a dedicated cron clone (overridable for tests).
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DIR="${ERROR_ISSUES_DIR:-/home/lem/error-to-issues}"
LOG="$DIR/error-to-issues.log"
mkdir -p "$DIR"
log(){ echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG" >&2; }

# Best-effort email to the admin on a hard failure — mirrors weekly_sdui_drift_check.sh's `alert()`.
# Reuses `_dispatch_email` inside the running `web_app` container rather than importing `cqc_lem`
# here: this script is designed to run from a dedicated cron clone whose python may not have the
# app's dependencies installed reliably (see the header above), so alerting itself must not become a
# new way for the cron to fail harder. `docker exec` needs no local cqc_lem env at all, so it works
# from that bare clone the same as it does from the dev checkout. If the container is unreachable
# (host down, compose stopped) the alert is skipped with a logged reason — never fails the run.
alert(){
  log "ALERT: $1"
  sudo -n docker exec -i web_app python - "$1" <<'PY' >>"$LOG" 2>&1 || log "alert email skipped (web_app unreachable) — see reason above"
import os, sys
msg = sys.argv[1]
try:
    from cqc_lem.utilities.email import _dispatch_email
    to = os.environ.get("ADMIN_EMAIL") or os.environ.get("LINKEDIN_EMAIL") or "christopher.queen@gmail.com"
    html = "<pre>" + msg.replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
    _dispatch_email(to, "LEM error->issues cron failed", html, text_content=msg, high_priority=True)
    print("alert emailed to", to)
except Exception as e:
    print("alert email failed (logged only):", e)
PY
}

# Python with `requests`. Prefer the stable dev-checkout venv; fall back to poetry inside REPO.
# (ERROR_ISSUES_PY overrides both — tests only; the cron never sets it.)
_DEV_VENV_PY="/home/lem/linkedin_engagement_manager/.venv/bin/python"
if [ -n "${ERROR_ISSUES_PY:-}" ]; then PY=("$ERROR_ISSUES_PY")
elif [ -x "$_DEV_VENV_PY" ]; then PY=("$_DEV_VENV_PY"); else PY=(poetry run python); fi

# Personal API key (query:read). The purpose-scoped POSTHOG_QUERY_API_KEY wins, with the shared
# POSTHOG_PERSONAL_API_KEY as the fallback (issue #1453) — the same precedence the Python side
# applies, so exporting either one here is enough. Env wins; otherwise read it off the box's prod
# env file. Both are exported: posthog_error_issues.py does the actual choosing.
LEM_ENV_FILE="${LEM_ENV_FILE:-/opt/lem/.env}"   # overridable for tests, same as stack_watchdog.sh
_env_value(){ sudo -n grep -E "^$1=" "$LEM_ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'"; }
if [ -z "${POSTHOG_QUERY_API_KEY:-}" ]; then
  POSTHOG_QUERY_API_KEY=$(_env_value POSTHOG_QUERY_API_KEY)
  export POSTHOG_QUERY_API_KEY
fi
if [ -z "${POSTHOG_PERSONAL_API_KEY:-}" ]; then
  POSTHOG_PERSONAL_API_KEY=$(_env_value POSTHOG_PERSONAL_API_KEY)
  export POSTHOG_PERSONAL_API_KEY
fi
if [ -z "${POSTHOG_QUERY_API_KEY:-}" ] && [ -z "${POSTHOG_PERSONAL_API_KEY:-}" ]; then
  log "No POSTHOG_QUERY_API_KEY or POSTHOG_PERSONAL_API_KEY set — skipping. Add one to $LEM_ENV_FILE to enable."
  exit 0
fi

log "=== error->issues scan start ==="
cd "$REPO" || { alert "error->issues cron: repo not found: $REPO"; exit 1; }
"${PY[@]}" scripts/posthog_error_issues.py --apply "$@" 2>&1 | tee -a "$LOG"
rc=${PIPESTATUS[0]}
log "=== error->issues scan done (exit $rc) ==="
if [ "$rc" != "0" ]; then
  alert "error->issues cron failed (exit $rc). Tail of $LOG:
$(tail -n 40 "$LOG")"
fi
exit "$rc"
