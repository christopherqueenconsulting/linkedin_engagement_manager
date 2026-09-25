# PostHog PR takeover

PostHog's self-driving GitHub App (`posthog[bot]`, branches `posthog-self-driving/*`) opens PRs
against this repo from its Inbox reports. After opening one it keeps working it: it answers review
comments (our `Claude Code Review` workflow posts one on every push) and re-checks CI on a
~15-minute timer, pushing fixes. **PostHog bills for each of those iterations**, and all of them are
work `lem-agentd` already does. The changes themselves are worth keeping — five PostHog PRs merged
before this existed — so the process keeps the change and moves the iteration to our pipeline.

**The ONE place:** `scripts/posthog_pr_takeover.py`, run by the
`lem-posthog-takeover.{service,timer}` pair in `scripts/agent-pipeline/systemd/` every 5 minutes.

## What a takeover does

For each OPEN PR that passes the identity check, in this order:

| Step | Action | Resumes by finding |
|---|---|---|
| 1 | File an issue titled as the PR, labelled `agent:working` + `priority:medium`, with a `<!-- lem-posthog-takeover pr=N -->` marker and a visible `posthog-pr: #N` line | the marker, at the start of a line |
| 2 | Create `feature/claude-issue-<issue>` at the PR's head commit (one `POST git/refs`, no clone) | the branch by name — an existing one is never rewritten |
| 3 | Open a PR from that branch, body ours (`Closes #<issue>`), labelled `agent:working` | any PR whose head is that branch |
| 4 | Close the PostHog PR, THEN comment where the work went, then delete its branch | — |

Every step is idempotent, so a crash at any point is repaired by the next timer tick. A dry run
(`python3 scripts/posthog_pr_takeover.py`, no `--apply`) prints the plan and changes nothing.

## Why these labels, and why the pipeline picks it up

The takeover leaves GitHub in exactly the state a `start` run leaves it in, so no daemon change was
needed:

- **The PR** carries `agent:working`. `Daemon.reconcile` queries `("agent:working", "pr")`, and the
  PR lanes that follow — rebase (row 23), fix (34), selfreview (37), merge (38) in
  [`agent-pipeline-v2.md`](agent-pipeline-v2.md) §4 — carry no privilege-granting lane label, so
  `v2_trust_ok` gates them on `pr_is_upstream` alone. The copied branch lives in this repo, so it
  passes. **Label provenance does not matter here**: whichever identity runs the script works.
- **The issue** carries `agent:working` with an open linked PR, which is row 18
  (`working_claim_has_work`) — it waits on its PR and closes when it merges. It deliberately does
  NOT carry `agent:ready`: that label grants a `start` run and is checked for provenance
  (`label_actor_trusted`), and there is nothing to start.
- `priority:medium` keeps the triage cron from treating it as unstructured.

An older PostHog branch that conflicts with `main` is not a takeover problem: the PR reads `DIRTY`
and row 23 dispatches `rebase`.

## Identity and untrusted text

A PR is taken over only if **every** field says PostHog opened it: login `posthog[bot]`, numeric
user id `206114724`, account type `Bot`, head repository = this repo, head branch starting
`posthog-self-driving/`, base `main`. A missing field refuses. A PR that fails the check after
naming `posthog[bot]` is logged at WARNING and left alone; a human PR is skipped silently.

The PostHog PR body is **data, never instructions** (`contribution-security.md` §3). It is copied
into the issue only inside a collapsed quote: every line prefixed `> ` (so it can never carry a
line-anchored marker and forge a takeover of another PR), `<!--` escaped (so it cannot hide text),
and `@` broken with a zero-width space (so copying it pings nobody). The takeover PR's body is ours
alone — PostHog prose never reaches it.

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
  strongest signal available from GitHub, and this process does both within one timer tick.
- The self-driving setup docs expose one knob: Inbox **settings → PR generation**, which turns PR
  creation off or raises the report priority an agent needs before it opens one
  (<https://posthog.com/docs/self-driving/setup>). That stops NEW PRs; it is the owner's call.
- PostHog's own tracker describes the CI follow-up as a ~15-minute timer reset by activity
  (PostHog/posthog#103751), which is why the timer runs every 5 minutes.

## Operating it

```bash
python3 scripts/posthog_pr_takeover.py                 # dry run — what would be taken over
python3 scripts/posthog_pr_takeover.py --apply --pr N  # one PR, by hand
journalctl -u lem-posthog-takeover.service -n 50       # what the timer did
```

Enabling (owner): set `POSTHOG_TAKEOVER_ENABLED=1` in `/home/lem/agent-pipeline/config.env`, ship
the units with `scripts/agent-pipeline/install.sh --sync`, then
`sudo systemctl enable --now lem-posthog-takeover.timer`. The service unsets `GH_TOKEN`, so it acts
as the stored `gh auth login` identity. Like `lem-triage-hourly`, it runs the script from
`/home/lem/linkedin_engagement_manager`, so that checkout must be on a branch that has it.
