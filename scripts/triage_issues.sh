#!/usr/bin/env bash
# Daily issue triage cron (host cron, runs as `lem`) — issue #748.
#
# Finds open GitHub issues missing milestone, priority:*, flow, or topical labels; runs the
# impact-first triage prompt against `lem-medium`; and writes a dated report to docs/triage/.
# Mutations (label/milestone edits) only happen with --apply.
#
# Cron (as `lem`):
#   0 9 * * * /home/lem/linkedin_engagement_manager/scripts/triage_issues.sh --apply
set -uo pipefail
export PATH="/home/lem/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export HOME="${HOME:-/home/lem}"

# Self-locate the repo root so this can run from a dedicated cron clone (overridable for tests).
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DIR="${TRIAGE_DIR:-/home/lem/triage}"
LOG="$DIR/triage.log"
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
    _dispatch_email(to, "LEM daily triage", html, text_content=msg, high_priority=False)
    print("alert emailed to", to)
except Exception as e:
    print("alert email failed (logged only):", e)
PY
}

# Python with the `openai` client. Prefer the stable dev-checkout venv; fall back to poetry.
_DEV_VENV_PY="/home/lem/linkedin_engagement_manager/.venv/bin/python"
if [ -x "$_DEV_VENV_PY" ]; then PY=("$_DEV_VENV_PY"); else PY=(poetry run python); fi

# Personal API key (not strictly required; the cron can run in deterministic-only mode).
if [ -z "${LITELLM_MASTER_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ]; then
  LITELLM_MASTER_KEY=$(sudo -n grep -E '^LITELLM_MASTER_KEY=' /opt/lem/.env 2>/dev/null \
    | cut -d= -f2- | tr -d '"' | tr -d "'")
  export LITELLM_MASTER_KEY
fi

log "=== daily issue triage start ==="
cd "$REPO" || { log "repo not found: $REPO"; exit 1; }

# Fast-forward the cron clone so decisions reflect current labels/milestones.
git pull -q --ff-only >>"$LOG" 2>&1 || log "repo fast-forward skipped (clone not on a clean main?)"

# Default to dry-run unless the cron was explicitly invoked with --apply.
MODE="${1:---dry-run}"
REPORT_DIR="$REPO/docs/triage"
mkdir -p "$REPORT_DIR"

"${PY[@]}" "$REPO/scripts/triage_issues.py" "$MODE" \
  --report-dir "$REPORT_DIR" \
  --repo "${TRIAGE_REPO:-christopherqueenconsulting/linkedin_engagement_manager}" \
  >>"$LOG" 2>&1
rc=$?

log "=== daily issue triage done (exit $rc) ==="

# Exit 2 means dry-run had pending changes — not a failure for the cron, but worth noting.
if [ "$rc" -eq 1 ]; then
  alert "Daily issue triage failed — see $LOG"
fi
exit "$rc"
