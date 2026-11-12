# Owner review on `main`: the Self-Modification Gate, not code-owner review

**Owner ruling, 2026-11-12:** the **Pipeline Self-Modification Gate** is the enforced owner review on
`main`. GitHub's code-owner review is switched off. The gate's scope is `.github/CODEOWNERS`.

## What was wrong before

Earlier docs said `require_code_owner_reviews: true` with `required_approving_review_count: 0`
"enforces nothing". That was a 2026-08-04 measurement, taken while the pipeline still authenticated
as the owner, so every PR was owner-authored. It is **wrong today**. GitHub does enforce the
setting: every PR touching a CODEOWNERS path needs `@gitchrisqueen`'s approval, whatever the count
says. Since #1251 the pipeline is its own GitHub App, so its PRs are not owner-authored. That is the
`BLOCKED` the v2 daemon parks as `awaiting_owner_review` (#1501).

GitHub's version cannot express the two exceptions this repo needs:

- **The owner's own PRs.** GitHub never lets an author approve their own PR. So code-owner review
  blocked the only human maintainer on their own owned-path changes (#1692).
- **Dependabot's mechanical bumps.** For example #2540 (`.github/dependabot.yml`) and every action
  or base-image bump sat `BLOCKED` waiting for a human to approve a version number.

## The arrangement now

| | Code-owner review (off) | Self-Modification Gate (required) |
|---|---|---|
| Scope | CODEOWNERS | CODEOWNERS, plus `PROTECTED` in the script (the pipeline, workflows, the gate's own script and test) |
| Owner-authored PR | blocked: GitHub forbids self-approval | **passes** |
| Pipeline App / any other author | needs owner approval | needs an **APPROVED review from `gitchrisqueen`** |
| `dependabot[bot]` | needs owner approval | passes only when the diff changes nothing but `uses: x@ref` lines (workflows), `FROM <image>:<tag>` / `FROM <image>@sha256:…` lines (Dockerfiles) or `image: <name>:<tag>` lines (`docker-compose*.yml`). Anything else in an owned path needs the owner |
| Unowned paths (`poetry.lock`, `pyproject.toml`, `package*.json`, …) | no review | no review |

The logic is `scripts/pipeline_selfmod_gate.py`, tested in `tests/unit/test_pipeline_selfmod_gate.py`.
The wiring is `.github/workflows/pipeline-selfmod-gate.yml`, tested in
`tests/unit/test_pipeline_selfmod_gate_workflow.py`.

**Fail-closed rules:**

- A missing, unreadable or empty CODEOWNERS refuses every author.
- A Dependabot diff that does not parse as a pure version move needs the owner.
- A file list shorter than the PR's `changedFiles` count refuses.
- A rename's source path is judged as well as its destination.

**It judges with the base branch's code.** The workflow checks out `pull_request.base.sha` and reads
the PR's files and diff as data (`gh pr view`, `gh pr diff`). A PR therefore cannot rewrite the
script, or shrink CODEOWNERS, to approve itself. The workflow YAML itself still runs from the PR, as
every `pull_request` workflow does. That file is protected by the pipeline credential having no
`workflows` permission (`docs/contribution-security.md`).

**Merge queue.** `main` merges through a queue, and a required check that never reports on
`merge_group` blocks the queue. The gate's `merge_group` run reports the same job name and passes.
The verdict was decided on the PR before it could be enqueued, and no approval can change inside the
queue.

**The daemon is unchanged.** The gate is deliberately **not** in the pipeline's own required-check
copies (`lemd/github.py` `REQUIRED_CHECKS`, `tick.sh` `REQUIRED_CHECKS_JQ`). A red gate with every
pipeline-required check green reads as `BLOCKED`. That is the same `awaiting_owner_review` wait
(and the same one-time "Owner approval needed" comment) that code-owner review used to produce.
Adding the gate to those copies would turn "needs the owner" into a CI failure routed to a fix lane.

## Owner steps

Run these after the PR that ships this lands (`R=christopherqueenconsulting/linkedin_engagement_manager`):

```bash
# 1. require the gate
gh api -X POST repos/$R/branches/main/protection/required_status_checks/contexts \
  -f 'contexts[]=Pipeline Self-Modification Gate'
# 2. then turn code-owner review off
gh api -X PATCH repos/$R/branches/main/protection/required_pull_request_reviews \
  -F require_code_owner_reviews=false
```

Do them in this order. Between step 2 and step 1 nothing would hold an owned path. Rollback is the
reverse: set `require_code_owner_reviews=true` first, then remove the context with
`gh api -X DELETE …/required_status_checks/contexts -f 'contexts[]=Pipeline Self-Modification Gate'`.
