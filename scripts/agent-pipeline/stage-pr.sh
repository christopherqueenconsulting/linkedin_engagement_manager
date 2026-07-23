#!/usr/bin/env bash
# Stage the agent pipeline as a reviewable repo PR (additive only).
# Uses an isolated worktree so your dev checkout / current branch is untouched.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
REPO="/home/lem/linkedin_engagement_manager"
WT="/home/lem/agent-pipeline-pr"
BR="feature/claude-agent-pipeline"

cd "$REPO"
git fetch origin --prune
git worktree prune
rm -rf "$WT"
git worktree add "$WT" -b "$BR" origin/main

mkdir -p "$WT/scripts/agent-pipeline"
for f in tick.sh RUNBOOK.md install.sh classify-issues.sh README.md stage-pr.sh; do
  cp "$SRC/$f" "$WT/scripts/agent-pipeline/$f"
done
chmod +x "$WT/scripts/agent-pipeline/"*.sh

cd "$WT"
git add scripts/agent-pipeline
git commit -m "feat(ci): autonomous milestone pipeline (Claude Max implementer, Copilot reviewer)

Local runner that drives roadmap issues (Milestones 7-12, #382-406) to merged+deployed:
Claude on the Max subscription implements each issue in an isolated git worktree, opens a
PR, fixes CI and addresses Copilot review comments, and auto-merges green low-risk PRs
(risky classes are held for human merge). Copilot stays review-only. Serial, cap=1, paced
to Max limits. Deploy rides the existing merge->release-please->build->deploy-vps pipeline.

Additive only; the runner is installed on the box at /home/lem/agent-pipeline. This commit
version-controls the source for review/transparency. Starts paused.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"

git push -u origin "$BR"

gh pr create --base main --head "$BR" \
  --title "feat(ci): autonomous milestone pipeline (Claude Max implementer, Copilot reviewer)" \
  --body "$(cat <<'BODY'
## What
Version-controls the **autonomous milestone pipeline** that drives roadmap issues (Milestones 7–12, #382–406) to merged & deployed. Additive only — no existing files changed.

## How it works
- **Implementer = Claude on the Max subscription** (runs on the VPS under `~/.claude`, **no API cost**). Works one issue at a time in an isolated git worktree → opens a PR → fixes failing CI → addresses **Copilot** review comments → auto-merges when green.
- **Reviewer = GitHub Copilot only** (free). Copilot never writes code here.
- **Serial (cap=1)**, priority order M7→M12, paced to Max limits (waits/resumes on usage caps).
- **Auto-merge green, hold risky.** `risk:migration` / `risk:security` / `risk:live-linkedin` / `risk:product-decision` are built to green then handed to a human to merge. Merge → existing release-please → build → `deploy-vps` (auto-rollback on health fail).
- **Escalation:** `needs-human` + assignee when stuck (≤4 CI-fix attempts), needs live-LinkedIn, or needs a decision.

## Files (`scripts/agent-pipeline/`)
- `tick.sh` — driver: advances the pipeline one step per cron tick
- `RUNBOOK.md` — the start/fix/review/escalate instructions Claude follows
- `install.sh` — installs the runner to `/home/lem/agent-pipeline` (paused) + hourly cron
- `classify-issues.sh` — labels the roadmap issues (already applied)
- `README.md` — operator docs & controls

## Safety
Every PR must pass **Unit Tests, Integration Tests, GitGuardian** + Copilot review before merge. Risky classes never auto-merge. Instant pause via `touch /home/lem/agent-pipeline/PAUSED`.

## Note
`.github/workflows/claude-code-review.yml` (the old API-key-based Claude reviewer) is **dormant** — no `ANTHROPIC_API_KEY` secret — since reviews are handled by Copilot. Consider removing it in a follow-up to avoid accidental future API spend.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
)"

echo
echo "PR opened. Review it, then merge when ready. To clean up the worktree later:"
echo "  git -C $REPO worktree remove $WT"
