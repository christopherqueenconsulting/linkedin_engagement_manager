# Branch Cleanup — Two Layers, One 48-Hour Grace

The repo accumulated 648 head-branch refs through v0.106.0 because nothing was deleting them. Most
came from the agent pipeline's per-issue branches; some came from closed-not-merged PRs that never
got cleaned; some were orphan `worktree-agent-*` and `feature/claude-issue-*` references from
agents that ran to completion but never wrote a manifest of what to drop. Two layers solve it.

## TL;DR

| Layer | What it catches | How |
|---|---|---|
| **Auto-delete-on-merge** (repo setting) | Every PR merged AFTER 2026-07-28 — the head branch disappears ~30s after the merge button | `delete_branch_on_merge=true` (one-line repo setting) |
| **Weekly orphan sweep** (`.github/workflows/stale-branches.yml`) | Orphan branches, closed-without-merge PR branches, anything the setting didn't catch because the PR merged before the setting was flipped | Mon 06:00 UTC cron + `actions/github-script` |

The **one-time manual sweep** in `docs/branch-cleanup-audit-2026-07-28.md` cleaned out the ~491 stale
branches that already existed before the layers above were turned on. That manifest is the recovery
record — see [Recovery](#recovery) below.

## The 48-hour grace window

**Both layers** hold any branch whose tip committer date is younger than 48 hours, regardless of
PR state or author. This is the protection for active agents: a fresh `feature/claude-issue-NNN`
branch is held even if it has no PR yet, even if its tip is ahead of main. The grace period resets
on every new commit, so an in-flight agent has 48h of breathing room per push.

The reasoning: an agent working through a milestone issue may take more than a working day per PR
across review cycles. 48h catches the realistic "I came back to this yesterday" case without
making the cron fight a live agent.

## The EXEMPT list — branches that are NEVER auto-deleted

`main`, `master`, `develop`, plus:

- `release-please--branches--main` — the release-please bot's working branch (the bot owns it)
- `feature/release-please-dedup` — active PR #777, the new release-please config
- `ci/claude-release-token-split` — release-token plumbing
- `chore/pipeline-sync-and-release-automerge` — release-pipeline sync
- `fix/release-pipeline-reliability` — release-pipeline reliability
- `hotfix/dashboard-stats-month-boundary` — long-lived hotfix
- `milestone/*` — milestone boundary branches
- `worktree-agent-*` — agent-pipeline worktree branches (orphans from the runner, but the agent
  may come back to them; the EXEMPT regex catches them all rather than a per-name list)

Encoded in two places that MUST stay in sync:
- `scripts/branch_cleanup.py` → `EXEMPT_RE`
- `.github/workflows/stale-branches.yml` → `EXEMPT` constant in the github-script step

When you add a new long-lived branch class (e.g. `experiments/*`), update BOTH. There's no
test that catches drift — keep them aligned by hand.

## Per-class decision flow

```
branch tip is <48h old?         → HELD (grace window)
matches EXEMPT_RE?              → HELD (release/milestone/agent)
PR is MERGED, branch >48h?      → DELETE
PR is CLOSED, branch >48h?      → DELETE (PR records the rejection)
PR is OPEN, branch >48h?        → SURFACE for review (author paused?)
no PR, branch >48h?             → SURFACE for review (orphan)
tip == main?                    → DELETE
```

`SURFACE for review` means the branch goes into the manifest's per-branch section and is NOT
auto-deleted. The one-time manual sweep does the same: orphans stay until a human opts them in
via `--keep-class-c <branch>` on a follow-up run.

## Opting a branch out of cleanup

If a branch needs to stick around indefinitely (long-lived feature flag branch, retrospective
work, etc.):

1. Rename it into `milestone/<name>` — the EXEMPT regex catches it forever.
2. OR add it to the EXEMPT regex in BOTH places (`scripts/branch_cleanup.py` + the workflow).

There is no per-branch "keep" label — the regex is the single source of truth.

## Recovery

The one-time sweep on 2026-07-28 recorded every deleted branch's tip SHA in
[`docs/branch-cleanup-audit-2026-07-28.md`](./branch-cleanup-audit-2026-07-28.md). To resurrect
a deleted branch, anyone with push access can:

```bash
git push origin <tip_sha>:refs/heads/<branch_name>
```

The reflog (`git reflog`) works locally too, within the local retention window. If the
auto-delete-on-merge ever deletes a branch you wanted to keep, the merge commit on `main` is
the source of truth — `git log --merges --first-parent main` will find the merge, and the PR's
URL is in the merge commit message.

## Future sweeps

Re-running `scripts/branch_cleanup.py --manifest-only` is the canonical manual re-sweep — it
regenerates the manifest without deleting anything. The weekly GitHub Actions workflow handles
the automated case between manual runs.

If the weekly workflow starts deleting branches you wanted to keep, the fastest fix is:

1. Re-push the branch from the merge commit: `git push origin <merge_sha>:refs/heads/<name>`
2. Rename it into the EXEMPT regex (`milestone/<name>`) so it doesn't get re-deleted
3. Or update the EXEMPT regex in both places

## Adding new branch patterns

When the agent pipeline introduces a new branch prefix (e.g. `experiments/<agent-id>`), the
EXEMPT regex is the place to update it. Make the change in both files in the same PR so they
stay aligned. There's no drift detector — keep them in sync by hand.