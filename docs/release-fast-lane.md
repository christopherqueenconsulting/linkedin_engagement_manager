# The `release:now` fast lane

## Why it exists

Releases are batched 4× daily (05/11/17/23 UTC). That is deliberate — one deploy per batch instead
of one per merge — but it is not free. Measured 2026-07-31 across the last 20 releases: a release PR
waits a **median of 168 minutes**, p90 **339 minutes**, worst case **408 minutes**. That is the right
price for a docs tweak and the wrong price for a bug a user is hitting right now.

`release:now` on a pull request releases it **as soon as it merges**, instead of at the next window.
Everything else about the pipeline is unchanged: the same CI, the same merge queue, the same
blue/green cutover, the same auto-rollback.

## How to use it

```bash
gh pr edit <PR> --repo christopherqueenconsulting/linkedin_engagement_manager --add-label 'release:now'
```

Apply it **before** the PR merges. The label is read at merge time; adding it afterwards does
nothing (use `gh workflow run release-auto-merge.yml` instead, which releases whatever is pending).

## When agents may apply it — the policy

**Agents may add `release:now` on their own judgement, without asking, when the change is either:**

1. **High priority** — the issue carries `priority:high`, or is labelled `bug` + `feedback-loop`
   (i.e. a real user reported it), or
2. **High visibility** — a user would notice the difference on their next visit: a broken or
   incorrect UI, a failing core loop (posting, commenting, DMs), broken auth/login, a data-integrity
   bug, or a fix to something currently emitting errors in production.

**Agents must NOT add it for:**

- docs, comments, tests, or refactors with no behaviour change
- dependency bumps (Dependabot has its own path)
- `priority:low` / `cleanup` / `chore` work
- anything behind a disabled feature flag — it isn't reaching users yet, so it can wait
- a change the agent could not verify (no test, no live validation) — an unverified change is
  exactly the one that should sit behind a normal window
- **more than one open PR at a time.** If you already fast-laned something today, batch the rest.
  The fast lane's value comes from being rare.

**Always allowed regardless of the above:** a revert, or a fix for something actively broken in
production. Getting back to a known-good state is never worth a 3-hour wait.

When an agent uses it, it should say so in the PR body in one line — *"Labelled `release:now`:
user-visible commenting failure, priority:high"* — so the choice is auditable rather than silent.

## What actually happens

1. PR merges with the label.
2. `release-auto-merge.yml` fires on `pull_request_target: closed`, sees `merged == true` and the
   label, and waits (up to 5 min) for release-please to open/refresh the release PR — release-please
   is reacting to the same merge, so the PR often does not exist yet.
3. It enables auto-merge on that release PR. The release PR still runs its own CI and still goes
   through the merge queue, so **a fast-laned release can never ship a red build**.
4. Tag → `Build & Deploy Release` → blue/green cutover.

A `concurrency` group serialises this: three `release:now` PRs merging together enqueue one release,
not three.

If release-please never opens a PR within the wait window the job emits a warning and exits 0 — the
change simply ships in the next scheduled window. The fast lane can delay a release; it cannot break
one.

## What it does not do

- It does **not** skip CI, the merge queue, or any required check.
- It does **not** bypass the blue/green cutover or auto-rollback.
- It does **not** release a specific PR in isolation — release-please batches by design, so a
  fast-laned release ships everything currently on main. That is usually fine; if it is not, the
  problem is that something unfinished is already on main, and that is the thing to fix.

## Related

- `docs/zero-downtime-deploys.md` — the cutover itself
- `.github/workflows/release-auto-merge.yml` — the implementation
- Redeploying or rolling back an existing tag is a different operation:
  `gh workflow run deploy-vps.yml -f tag=vX.Y.Z`
