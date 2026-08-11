# LEM Autonomous Milestone Pipeline

Drives GitHub issues (Milestones 7–12) to merged & deployed, using **Claude on your Max
subscription** (no API cost) as the implementer and **GitHub Copilot** as the free reviewer.

> **This describes v1 (`tick.sh`), which is now only the failsafe.** The live runner is the
> `lem-agentd` daemon — see [`docs/agent-pipeline-v2.md`](../../docs/agent-pipeline-v2.md) for how
> work actually flows today, and `v2/README.md` for the operator commands. The v1 flow below still
> documents what the failsafe does when the daemon's heartbeat goes stale.

## Flow — v1 failsafe (one issue at a time)
```
cron (every 15 min) → tick.sh --failsafe advances the pipeline by ONE step:
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
| **Live status** | `/home/lem/agent-pipeline/status.sh` (add `--watch` for a refreshing dashboard) |
| Watch | `tail -f /home/lem/agent-pipeline/logs/tick-*.log` |
| Dry-run | `DRY_RUN=1 /home/lem/agent-pipeline/tick.sh` |
| One tick now | `/home/lem/agent-pipeline/tick.sh` |
| Kill switch | `touch PAUSED` + `crontab -e` (remove the tick line) |

## Live status (`status.sh`)
Answers "how many agents are running, on what, and is anything wrong?" in one screen: the live
`claude -p` processes with their slot / mode / issue / branch / lane and **what each one is doing
this minute** (read from its own transcript), slot occupancy from the flocks, lane capacity, the
backlog, open PRs, a rollup of recent tick outcomes, and a `NEEDS ATTENTION` list (stranded
`agent:working` claims, a lane paused on a usage limit, a PR the merge queue keeps dropping, an
agent about to hit its 45m timeout). It also lists **every other `claude` process on the box** —
an interactive or remote-control session works no issue, but draws on the same Max subscription as
the claude lane, so it belongs in any count of what is running (and in any usage-limit post-mortem).

It is **read-only**: it never dispatches, never spends a probe call, and never mutates lane state —
so it is safe to leave running in `--watch`. `--json` emits the same data for monitoring.

## Safety net
Every PR must pass **Unit Tests, Integration Tests, GitGuardian** (branch protection) plus Copilot
review before it can merge. Deploy has auto-rollback to `.last_good_tag` on health-check failure.
Risky classes never auto-merge. You can pause instantly with the `PAUSED` file.
