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
| **Weekly worktree sweep** (`scripts/worktree_cleanup.sh`) | `git worktree` REGISTRATIONS left behind by finished agents — the branch layers never touch these | Mon 07:00 UTC systemd timer on the agent host (Actions cannot see them); see [The third layer](#the-third-layer--worktree-registrations) |

The **one-time manual sweep** in `docs/branch-cleanup-audit-2026-07-28.md` cleaned out the ~491 stale
branches that already existed before the layers above were turned on. That manifest is the recovery
record — see [Recovery](#recovery) below.

## The 48-hour grace window

**All three layers** hold anything whose tip committer date is younger than 48 hours, regardless of
PR state or author. This is the protection for active agents: a fresh `feature/claude-issue-NNN`
branch is held even if it has no PR yet, even if its tip is ahead of main. The grace period resets
on every new commit, so an in-flight agent has 48h of breathing room per push.

The reasoning: an agent working through a milestone issue may take more than a working day per PR
across review cycles. 48h catches the realistic "I came back to this yesterday" case without
making the cron fight a live agent.

## The EXEMPT list — branches that are NEVER auto-deleted

`main`, `master`, `develop`, plus:

- `release-please--branches--main` — the release-please bot's working branch (the bot owns it)
- `release/*` — release branches

The 2026-07-28 historical pins (`milestone/*`, `worktree-agent-*`, `feature/release-please-dedup`
and four named release/pipeline branches) were deleted by hand on 2026-08-03 with owner approval —
all of their content is on `main` — and dropped from the regex. Agent worktree branches don't need
a pin: a live push is inside the 48h grace, and an orphan (no PR ever) is surfaced, never deleted.

Encoded in two places that MUST stay in sync:
- `scripts/branch_cleanup.py` → `EXEMPT_RE`
- `.github/workflows/stale-branches.yml` → `EXEMPT` constant in the github-script step

When you add a new long-lived branch class (e.g. `experiments/*`), update BOTH. There's no
test that catches drift — keep them aligned by hand.

## Per-class decision flow

```
branch tip is <48h old?         → HELD (grace window)
matches EXEMPT_RE?              → HELD (main/release-please/release)
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

## The third layer — worktree registrations

The two layers above clean up *branches*. They do not touch the `git worktree` **registrations** that
CLAUDE.md's one-worktree-per-agent rule creates, and nothing else did either: by 2026-09-01 the repo
had **292** registered worktrees, 261 of them merged or branch-gone. `scripts/worktree_cleanup.sh`
is the sweep for that half.

A registration is removable when its branch is **gone from origin** (merged, or swept by the weekly
branch cron) or its HEAD is already an **ancestor of origin/main**. Removing one deletes the working
directory and the registration — never the local branch ref, so committed work stays reachable
through `refs/heads`. The only thing a removal can destroy is *uncommitted* content, which is why
the script never passes `--force`.

Five fail-closed protections. A worktree is HELD, never removed, when it:

1. is the primary checkout, is locked, or is the tree the script was invoked from;
2. has uncommitted changes — tracked modifications **or untracked files**;
3. has a live process whose cwd is inside it — a running agent lane;
4. has a branch tip younger than `GRACE_HOURS` (default 48, the same grace the branch layers use);
5. is a **detached HEAD not merged into origin/main** — it has no branch ref to outlive the removal,
   so "branch gone from origin" proves nothing about it; only reachability from main does.

Held-because-uncommitted trees are reported for a human decision, in copy-pasteable form (path,
first status lines, the inspect command). The script resolves none of them.

Three conditions disable removals for the **whole run** — it reports and deletes nothing:

- no `--apply` (the default: a bare invocation is a dry run);
- `git fetch --prune origin` failed, or `--no-fetch` was passed. Both removal tests read *local*
  remote refs, so without a fresh fetch "gone from origin" and "ancestor of origin/main" are a
  stale answer to a live question;
- the live-process detector is blind. It walks `/proc/*/cwd` and self-tests against its own cwd:
  with no readable procfs, "no process found" really means "cannot look", which would turn
  protection 3 from fail-closed into fail-open on the one check covering a running agent lane.

```bash
scripts/worktree_cleanup.sh              # report only — the default
scripts/worktree_cleanup.sh --apply      # actually remove
```

**Where "weekly" comes from.** Not GitHub Actions: a hosted runner cannot see worktrees registered
on the agent host, so unlike the branch half this cannot live in `stale-branches.yml`. The schedule
is a systemd timer on the box, committed alongside the script:

```
scripts/systemd/lem-worktree-sweep.timer     # Mon 07:00 UTC, Persistent=true
scripts/systemd/lem-worktree-sweep.service   # User=lem, ExecStart=... --apply
```

Install it the same way as the watchdog units:

```bash
sudo cp scripts/systemd/lem-worktree-sweep.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now lem-worktree-sweep.timer
```

`--apply` is carried by the **unit**, never by the script's default, so a fat-fingered manual run
cannot delete anything. 07:00 UTC is one hour after the Mon 06:00 UTC branch sweep, so the branch
deletions have already landed and this run's fetch sees them as gone from origin.

The run log is the journal — every held tree with its reason, plus a one-line summary of counts per
bucket:

```bash
journalctl -u lem-worktree-sweep.service | grep 'worktree sweep:'
# [2026-09-01T06:00:26Z] worktree sweep: mode=dry-run would-remove=0 failed=0 skipped=2 \
#   held(uncommitted=23 active=2 grace=5 live-branch=3 locked=3 unreadable=0)
```

A skip is always logged, including the primary checkout and the tree the script was invoked from —
silent truncation reads as "swept everything".

**A worktree whose directory was deleted by hand** is not this script's problem — `git worktree
prune` handles it, and the script calls it on both ends of the sweep.

Decision coverage lives in `tests/unit/scripts/test_worktree_cleanup.py`, which builds a throwaway
repo with a real worktree in each state and asserts on the report lines.

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