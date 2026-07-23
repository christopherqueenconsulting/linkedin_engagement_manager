#!/usr/bin/env bash
# Apply the "park held PRs" fix without pausing the live pipeline:
#  - copy the patched RUNBOOK to the box (no full re-install, so PAUSED is untouched)
#  - park the already-held PR #408 + issue #382 so the pipeline proceeds to the next issue
#  - tidy #402's redundant priority label
set -uo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
SLUG="christopherqueenconsulting/linkedin_engagement_manager"

cp "$SRC/RUNBOOK.md" /home/lem/agent-pipeline/RUNBOOK.md
echo "runbook updated on box."

gh pr edit 408 --repo "$SLUG" --add-label agent:blocked --remove-label agent:working >/dev/null 2>&1 && echo "PR #408 parked (agent:blocked)."
gh issue edit 382 --repo "$SLUG" --add-label agent:blocked --remove-label agent:working >/dev/null 2>&1 && echo "issue #382 parked."
gh issue edit 402 --repo "$SLUG" --remove-label priority:critical >/dev/null 2>&1 && echo "#402 priority tidied."

echo "done. Next tick will pick the next ready issue (#383)."
