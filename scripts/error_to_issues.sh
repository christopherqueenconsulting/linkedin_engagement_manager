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

# Python with `requests`. Prefer the stable dev-checkout venv; fall back to poetry inside REPO.
_DEV_VENV_PY="/home/lem/linkedin_engagement_manager/.venv/bin/python"
if [ -x "$_DEV_VENV_PY" ]; then PY=("$_DEV_VENV_PY"); else PY=(poetry run python); fi

# Personal API key (query:read). Env wins; otherwise read it off the box's prod env file.
if [ -z "${POSTHOG_PERSONAL_API_KEY:-}" ]; then
  POSTHOG_PERSONAL_API_KEY=$(sudo -n grep -E '^POSTHOG_PERSONAL_API_KEY=' /opt/lem/.env 2>/dev/null \
    | cut -d= -f2- | tr -d '"' | tr -d "'")
  export POSTHOG_PERSONAL_API_KEY
fi
if [ -z "${POSTHOG_PERSONAL_API_KEY:-}" ]; then
  log "No POSTHOG_PERSONAL_API_KEY set — skipping. Add it to /opt/lem/.env to enable."
  exit 0
fi

log "=== error->issues scan start ==="
cd "$REPO" || { log "repo not found: $REPO"; exit 1; }
"${PY[@]}" scripts/posthog_error_issues.py --apply "$@" 2>&1 | tee -a "$LOG"
rc=${PIPESTATUS[0]}
log "=== error->issues scan done (exit $rc) ==="
exit "$rc"
