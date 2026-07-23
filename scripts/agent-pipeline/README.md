# LEM Autonomous Milestone Pipeline

Drives GitHub issues (Milestones 7–12) to merged & deployed, using **Claude on your Max
subscription** (no API cost) as the implementer and **GitHub Copilot** as the free reviewer.

## Flow (one issue at a time)
```
cron (every 15 min) → tick.sh advances the pipeline by ONE step:
  • Dependabot PR failing (agent:depfix) → PRIORITY lane: Claude smart-triages the fix
                       (bump-caused → fix on branch; not-the-bump's-fault → main-side fix PR + rebase)
  • in-flight PR is CONFLICTING → Claude rebases it onto main (resolves conflicts, bumps migration #s)
  • no PR in flight  → pick next `agent:ready` issue (M7→M12, by priority) → Claude implements
                       in an isolated git worktree → pushes → opens PR (holds it if risky)
  • PR CI failing    → Claude fixes on the same branch (≤4 attempts, then escalates to you)
  • Copilot unresolved threads → Claude addresses + resolves each one
  • CI green + Copilot reviewed + threads resolved → runner enqueues the merge → release → deploy
  • risky / stuck / needs live-LinkedIn → label `needs-human`, assign you, stop
```
`tick.sh` only spends Max tokens when there's real work (implement / fix / address review).
CI-pending and merge-waiting ticks are cheap `gh` reads — no Claude call.

## Design choices
- **Subscription only.** Runs `claude` on the box under `~/.claude` (your Max login). No `ANTHROPIC_API_KEY`.
- **Copilot = review only** (free); it never writes code here.
- **Serial (cap=1).** One PR in flight; gentlest on Max limits. Change the concurrency by running
  tick more often — but keep cap=1 unless you add branch-conflict handling.
- **Isolated worktrees.** Never switches your dev checkout's branch.
- **Auto-merge green, hold risky.** Issues labeled `risk:migration` / `risk:security` /
  `risk:live-linkedin` / `risk:product-decision` are built to green then handed to you to merge.

## Labels
- `agent:ready` — eligible for pickup · `agent:working` — in flight · `agent:blocked` — parked
- `needs-human` — escalated & assigned to you
- `risk:*` — held at merge for your review

## Controls
| Action | Command |
|---|---|
| Go live | `rm /home/lem/agent-pipeline/PAUSED` |
| Pause | `touch /home/lem/agent-pipeline/PAUSED` |
| Watch | `tail -f /home/lem/agent-pipeline/logs/tick-*.log` |
| Dry-run | `DRY_RUN=1 /home/lem/agent-pipeline/tick.sh` |
| One tick now | `/home/lem/agent-pipeline/tick.sh` |
| Kill switch | `touch PAUSED` + `crontab -e` (remove the tick line) |

## Safety net
Every PR must pass **Unit Tests, Integration Tests, GitGuardian** (branch protection) plus Copilot
review before it can merge. Deploy has auto-rollback to `.last_good_tag` on health-check failure.
Risky classes never auto-merge. You can pause instantly with the `PAUSED` file.
