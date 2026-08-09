#!/usr/bin/env bash
# One-time installer for the LEM agent pipeline. Run as user `lem` on the VPS.
# Copies the runner to /home/lem/agent-pipeline, starts it PAUSED, and adds an hourly cron.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="/home/lem/agent-pipeline"

mkdir -p "$DEST/logs" "$DEST/work" "$DEST/lib"
cp "$SRC/tick.sh" "$DEST/tick.sh"
cp "$SRC/RUNBOOK.md" "$DEST/RUNBOOK.md"
# lib/ was NOT copied here until 2026-08-09, so tick.sh shipped while the helpers it sources did
# not. The box's copies drifted out of the repo's sight: `run_lane.sh` there had gained an export
# block (the #842 unattended-benchmark vars) that existed nowhere in git, and would have been
# destroyed by the first person who thought to sync lib/ the obvious way.
cp "$SRC"/lib/*.sh "$DEST/lib/"
chmod +x "$DEST/tick.sh"
touch "$DEST/PAUSED"   # start paused — nothing runs until you remove this file
echo "Installed to $DEST (PAUSED)."

# Add the tick cron for lem if not already present.
# Every 5 minutes, matching what the box actually runs — this said hourly while the installed
# crontab was `*/5`, so the documented cadence was 12x slower than the real one, and anyone
# reasoning about how long a pause takes to bite (or how much work a tick can start) got it wrong.
LINE="*/5 * * * * /home/lem/agent-pipeline/tick.sh >/dev/null 2>&1"
if crontab -l 2>/dev/null | grep -qF "/home/lem/agent-pipeline/tick.sh"; then
  echo "cron entry already present."
else
  ( crontab -l 2>/dev/null; echo "$LINE" ) | crontab -
  echo "cron entry added (every 5 minutes)."
fi

cat <<'EOF'

Next steps:
  1. Dry-run once to validate selection/state logic (no code changes, no Claude call):
       DRY_RUN=1 /home/lem/agent-pipeline/tick.sh ; tail -n 40 /home/lem/agent-pipeline/logs/tick-*.log
  2. Go LIVE:      rm /home/lem/agent-pipeline/PAUSED
  3. Pause again:  touch /home/lem/agent-pipeline/PAUSED
  4. Watch:        tail -f /home/lem/agent-pipeline/logs/tick-*.log
  5. Run one tick manually now (instead of waiting for cron):
       /home/lem/agent-pipeline/tick.sh
EOF
