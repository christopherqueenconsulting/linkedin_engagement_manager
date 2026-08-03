#!/usr/bin/env bash
# Weekly TODO->issues cron (host cron, runs as `lem`) — sibling of error_to_issues.sh.
#
# Sweeps the tracked tree for TODO/FIXME-class markers and "not yet implemented"-class phrases and
# files ONE deduped GitHub issue per unfinished-code comment, so shipped-but-partial code (the
# catch-up locator class of failure, see PR #964) gets tracked work instead of a comment nobody
# reads. Dedup marker: `todo-sweep-<hash>` in the issue body; closed issues count as judged.
#
# Cron (as `lem`, weekly — the error scan stays daily):
#   0 10 * * 0 git -C /home/lem/error-issues-cron fetch -q origin main && \
#     git -C /home/lem/error-issues-cron reset -q --hard origin/main; \
#     /home/lem/error-issues-cron/scripts/todo_to_issues.sh
set -uo pipefail
export PATH="/home/lem/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export HOME="${HOME:-/home/lem}"

# Self-locate the repo root so this can run from the dedicated cron clone (overridable for tests).
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DIR="${TODO_ISSUES_DIR:-/home/lem/todo-issues}"
LOG="$DIR/todo-to-issues.log"
mkdir -p "$DIR"
log(){ echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG" >&2; }

# Python: the script is stdlib-only, so the system interpreter is enough; prefer the dev venv when
# present for consistency with the sibling cron.
_DEV_VENV_PY="/home/lem/linkedin_engagement_manager/.venv/bin/python"
if [ -x "$_DEV_VENV_PY" ]; then PY=("$_DEV_VENV_PY"); else PY=(python3); fi

log "=== todo->issues sweep start ==="
cd "$REPO" || { log "repo not found: $REPO"; exit 1; }
"${PY[@]}" scripts/todo_issue_sweep.py --apply --repo-root "$REPO" "$@" 2>&1 | tee -a "$LOG"
rc=${PIPESTATUS[0]}
log "=== todo->issues sweep done (exit $rc) ==="
exit "$rc"
