#!/usr/bin/env bash
# Sync PR #407 (feature/claude-agent-pipeline) so the repo copy matches the corrected box runner.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
REPO="/home/lem/linkedin_engagement_manager"
WT="/home/lem/agent-pipeline-pr"
BR="feature/claude-agent-pipeline"

# Ensure the worktree exists and is on the PR branch, up to date with the remote.
if [ ! -d "$WT/.git" ] && ! git -C "$REPO" worktree list | grep -q "$WT"; then
  git -C "$REPO" fetch origin --prune
  git -C "$REPO" worktree prune
  rm -rf "$WT"
  git -C "$REPO" worktree add "$WT" -b "$BR" "origin/$BR"
fi
git -C "$WT" fetch origin "$BR"
git -C "$WT" checkout "$BR"
git -C "$WT" reset --hard "origin/$BR"

mkdir -p "$WT/scripts/agent-pipeline"
for f in tick.sh RUNBOOK.md install.sh classify-issues.sh README.md \
         stage-pr.sh apply-parking-fix.sh apply-review-gate-fix.sh sync-pr.sh; do
  cp "$SRC/$f" "$WT/scripts/agent-pipeline/$f"
done
chmod +x "$WT/scripts/agent-pipeline/"*.sh

cd "$WT"
if git diff --quiet && git diff --cached --quiet; then
  echo "Nothing to sync — PR #407 already matches the box."
  exit 0
fi
git add scripts/agent-pipeline
git commit -m "chore(ci): sync agent pipeline runner — runner-controlled merge + Copilot review gate

- Merge is now controlled by tick.sh (not GitHub eager auto-merge): a no-risk PR merges only
  after CI green AND Copilot reviewed the current head AND all Copilot review threads resolved.
- MODE=review addresses and resolves each Copilot thread (catches COMMENTED suggestions, not
  just CHANGES_REQUESTED).
- Risk-labeled PRs are parked (agent:blocked + needs-human) so the serial pipeline proceeds.
- Adds the operational helper scripts used on the box.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
git push origin "$BR"
echo "Synced. PR #407 now matches the box runner."
