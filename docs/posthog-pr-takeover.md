# PostHog PR takeover

PostHog's self-driving GitHub App (`posthog[bot]`, branches `posthog-self-driving/*`) opens PRs
against this repo from its Inbox reports. After opening one it keeps working it: it answers review
comments (our `Claude Code Review` workflow posts one on every push) and re-checks CI on a
~15-minute timer, pushing fixes. **PostHog bills for each of those iterations**, and all of them are
work `lem-agentd` already does. The changes themselves are worth keeping — five PostHog PRs merged
before this existed — so the process keeps the change and moves the iteration to our pipeline.

**The ONE place:** `scripts/agent-pipeline/v2/posthog_takeover.py`, spawned by the `lem-agentd`
daemon as `v2/actions/posthog_takeover.sh <N>`. There is **no timer and no process of its own**
(owner decision on #2189): it is event-driven off the webhook the pipeline's GitHub App already
receives.

## How it is triggered

```
PostHog opens/pushes a PR
  → App webhook `pull_request` (opened | reopened | synchronize | ready_for_review)
  → lemd.receiver stores it, keeping the PR's `author` + `head_ref` (trim_payload)
  → Daemon.drain_events: lemd/posthog.is_takeover_trigger() → number queued (in memory)
  → Daemon.takeover_posthog (every loop pass, after reconcile, before act; honours PAUSED/shadow)
  → Supervisor.dispatch_takeover → actions/posthog_takeover.sh N   (gh pool, 180s, no queue item)
  → posthog_takeover.py --apply --pr N   (re-reads the PR; decides WHETHER)
```

- **The payload is only a prompt.** `is_takeover_trigger` is a cheap pre-filter on the stored
  delivery (author, sender or head branch names PostHog). The action re-reads PR `N` from the REST
  API and applies the full identity check before any write; a closed/merged PR, a non-PostHog PR or
  an impostor does nothing. A false positive costs one short process.
- **Safety net, no new timer:** `Daemon.reconcile` (already running every 600s) makes ONE extra
  call, `gh pr list --author app/posthog --state open`, and queues whatever it returns. That covers
  a delivery lost while the receiver was down and a pending number a daemon restart forgot.
- **Why not a GitHub Actions workflow:** a PR opened with `GITHUB_TOKEN` never triggers
  `pull_request` workflows, so the required checks would not run on the takeover PR — and the
  pipeline credential has no `workflows` permission. The action runs under the pipeline's App
  identity (`common.sh`, which refuses rather than fall back to the owner login); a PR opened by an
  App installation token does trigger CI.
- **Not a `decide()` branch.** A PostHog PR carries no `agent:*` label, so it is never a queue item
  and never reaches `observe.decide()`; the takeover child carries the placeholder kind `posthog`,
  which `collect()` matches to no row. The PR it OPENS is an ordinary item from then on.
- A run that fails leaves the PR open, so the next reconcile relist re-queues it — no retry loop.

The App is already subscribed to `pull_request` (measured: the receiver log holds hundreds of
`stored pull_request/opened` rows, including #1916's on 2026-09-02). Old stored rows carry no
`author`; for them the filter falls back to `sender`, which for `opened`/`synchronize` is the App.

## What a takeover does

For the one PR, in this order:

| Step | Action | Resumes by finding |
|---|---|---|
| 1 | File an issue titled as the PR, labelled `agent:working` + `priority:medium`, with a `<!-- lem-posthog-takeover pr=N -->` marker and a visible `posthog-pr: #N` line | the marker, at the start of a line |
| 2 | Create `feature/claude-issue-<issue>` at the PR's head commit (one `POST git/refs`, no clone) | the branch by name — an existing one is never rewritten |
| 3 | Open a PR from that branch, body ours (`Closes #<issue>`), labelled `agent:working` | any PR whose head is that branch |
| 4 | Close the PostHog PR, THEN comment where the work went, then delete its branch | — |

Every step is idempotent, so a crash, a duplicate delivery or a late `synchronize` for a PR already
taken over is a no-op.

## Why these labels, and why the pipeline picks it up

The takeover leaves GitHub in exactly the state a `start` run leaves it in:

- **The PR** carries `agent:working`. `Daemon.reconcile` queries `("agent:working", "pr")`, and the
  PR lanes that follow — rebase (row 23), fix (34), selfreview (37), merge (38) in
  [`agent-pipeline-v2.md`](agent-pipeline-v2.md) §4 — carry no privilege-granting lane label, so
  `v2_trust_ok` gates them on `pr_is_upstream` alone. The copied branch lives in this repo.
- **The issue** carries `agent:working` with an open linked PR: row 18 (`working_claim_has_work`)
  — it waits on its PR and closes when it merges. It deliberately does NOT carry `agent:ready`,
  which grants a `start` run and is checked for provenance; there is nothing to start. Because the
  App files it, `author_trusted` accepts it via `GH_APP_BOT_LOGIN` if it is ever relabelled.
- `priority:medium` keeps the triage cron from treating it as unstructured.

An older PostHog branch that conflicts with `main` reads `DIRTY` and row 23 dispatches `rebase`. A
change touching a CODEOWNERS path reaches row 31 (`owner_review_required`) like any bot-authored PR.

## Identity and untrusted text

A PR is taken over only if **every** field says PostHog opened it: login `posthog[bot]`, numeric
user id `206114724`, account type `Bot`, head repository = this repo, head branch starting
`posthog-self-driving/`, base `main` (constants in `lemd/posthog.py`, shared by trigger and action).
A missing field refuses.

The PostHog PR body is **data, never instructions** (`contribution-security.md` §3). It is copied
into the issue only inside a collapsed quote: every line prefixed `> ` (so it can never carry a
line-anchored marker and forge a takeover of another PR), `<!--` escaped, and `@` broken with a
zero-width space (so copying it pings nobody). The takeover PR's body is ours alone.

**Protected paths are skipped.** A PR touching `.github/` or `scripts/agent-pipeline/` is logged and
left open for the owner: copying a workflow change needs the `workflows` permission the pipeline
does not hold, and `scripts/pipeline_selfmod_gate.py` exists so pipeline changes never merge
unattended.

## Retiring PostHog's branch

Deleted only once our branch provably contains PostHog's head, decided by GitHub's own ancestry
(`compare/<posthog-head>...<ours>`): `identical`/`ahead` → delete; `behind` (PostHog pushed after
the copy) → fast-forward ours with `force=false`, then delete; `diverged` or unreadable → keep it
and log a WARNING. Closing comes before the comment on purpose — a fresh comment on an OPEN PR is
exactly what PostHog's agent answers.

## Can PostHog be told to stop?

Findings, not configuration (nothing was changed in PostHog):

- There is no per-PR "stop" control documented. Closing the PR and deleting its branch is the
  strongest signal available from GitHub, and the takeover does both seconds after the PR opens.
- The self-driving setup docs expose one knob: Inbox **settings → PR generation**, which turns PR
  creation off or raises the report priority an agent needs before it opens one
  (<https://posthog.com/docs/self-driving/setup>). That stops NEW PRs; it is the owner's call.
- PostHog's own tracker describes the CI follow-up as a ~15-minute timer reset by activity
  (PostHog/posthog#103751); the event path lands well inside it.

## Operating it

```bash
python3 scripts/agent-pipeline/v2/posthog_takeover.py                 # dry run — every open PostHog PR
python3 scripts/agent-pipeline/v2/posthog_takeover.py --apply --pr N   # backfill one PR by hand
grep 'PostHog takeover' logs/v2-actions-*.log; tail logs/v2-posthog-takeover.log   # on the box
```

Shipping (owner): the change lives in `v2/lemd/*.py`, `v2/actions/` and `v2/*.py`, all copied by
`scripts/agent-pipeline/install.sh --sync`; then restart **both** units
(`sudo systemctl restart lem-agentd lem-agent-webhook`) — the receiver change (`author`/`head_ref`)
only takes effect in a restarted `lem-agent-webhook`, and until it does the `sender` fallback and the
reconcile relist still catch every PostHog PR.
