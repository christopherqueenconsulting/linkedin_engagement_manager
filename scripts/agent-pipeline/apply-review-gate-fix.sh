#!/usr/bin/env bash
# Apply the runner-controlled-merge / Copilot-review-gate fix to the live box install.
# Grabs the pipeline lock first so it can't overwrite files mid-tick.
set -uo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="/home/lem/agent-pipeline"

exec 9>"$DEST/lock"
if ! flock -n 9; then
  echo "A tick is currently running. Re-run this in a minute." >&2
  exit 1
fi

cp "$SRC/tick.sh"     "$DEST/tick.sh"     && chmod +x "$DEST/tick.sh"
cp "$SRC/RUNBOOK.md"  "$DEST/RUNBOOK.md"
echo "Updated tick.sh + RUNBOOK.md on the box."
echo "Merge is now runner-controlled: a PR merges only after CI green + Copilot reviewed head + all Copilot threads resolved."
