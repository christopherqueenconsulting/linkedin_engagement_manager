# CodeQL PR Gate (Option B)

The sweep PR (#781) fixes the 97 existing CodeQL alerts. This gate is the **prevention** half:
it runs on every PR and catches newly introduced `security-and-quality` / `security-extended`
Python alerts before they reach `main`.

## What it does

Workflow: `.github/workflows/codeql-pr-gate.yml`
Helper:   `scripts/codeql_pr_gate.py`
Config:   `.github/codeql/codeql-config.yml`

1. After the existing CodeQL workflows upload SARIF **for the commit being gated** (see
   *Analysis freshness* below), the gate queries
   `/repos/{owner}/{repo}/code-scanning/alerts` for both the PR head and the base ref.
2. It computes the **new** alerts introduced by the PR (by rule, file, line, and message — plus
   the alert number, see *Alert identity* below).
3. It buckets them:

### Auto-fixable / mechanical quality alerts

The gate tries to fix these automatically and pushes the result back to the PR branch:

| Rule | Fix strategy |
|------|--------------|
| `py/unused-import` | `ruff check --select F --fix` |
| `py/unused-global-variable` | `ruff check --select F --fix` |
| `py/unused-local-variable` | `ruff check --select F --fix` |
| `py/repeated-import` | `ruff check --select F --fix` |
| `py/multiple-definition` | `ruff check --select F --fix` |
| `py/implicit-string-concatenation-in-list` | Insert a comma between adjacent string literals in the flagged list |
| `py/empty-except` | Replace bare `except:` with `except Exception:` (only when the block is empty/`pass`) |

After fixes are applied, the workflow re-runs CodeQL (`security-extended` + `security-and-quality`)
on the fixed code with `upload: false` to verify the mechanical alerts are gone.

### Judgment / non-mechanical alerts

These are posted to the PR for human review and **fail the check**:

- `py/ineffectual-statement`
- `py/bad-tag-filter`
- Any other new quality alert not in the auto-fixable list

To dismiss a false positive, add an `lgtm` suppression comment on the flagged line, e.g.:

```python
except SomeExternalException:  # lgtm[py/empty-except]
    pass
```

### Security-severity alerts

Any alert with a `security_severity_level` (critical / high / medium / low) is **always blocking**.
They are never auto-fixed, never auto-dismissed, and must be reviewed by a human.

## Trigger

- `pull_request` targeting `main`
- `merge_group`
- `workflow_call` for reusable dispatch

## Permissions

The job needs:

- `actions: read` — not currently used, reserved for future workflow-run polling.
- `contents: write` — to push auto-fix commits back to the PR branch.
- `pull-requests: write` — to post the judgment/security alert summary.
- `security-events: read` — to read `/code-scanning/alerts` and `/code-scanning/analyses`.

The verification CodeQL step uses `upload: false`, so no SARIF write permission is required.

## Analysis freshness — "an analysis exists" is not "an analysis for THIS commit" (issue #904)

The gate and `CodeQL Advanced` both fire on `pull_request` with no `needs:` between them, so they
race. On every push after a PR's first, an analysis for `refs/pull/<n>/merge` already exists — from
the *previous* commit. The original wait polled only "does the ref have any analysis", so it
returned instantly and the gate then diffed the previous commit's alerts. Both directions are
wrong, and the second one is the dangerous one:

- **False red** — it reported alerts the push had already fixed (observed on #899: five alerts, all
  fixed, alert IDs byte-identical to the previous scan, one of them naming a variable binding that
  had been deleted). A bare re-run then passed with no code change, which teaches people to re-run
  on red instead of reading the report.
- **False green** — a genuinely new alert on the current commit is missed whenever the previous
  commit was clean. Nobody investigates a green gate, so this would never have been noticed.

The fix: the gate is told the SHA its ref resolves to and waits for *that commit's* analysis.

- The workflow passes `--head-sha`. For a PR this is `github.sha` — the ephemeral **merge** commit
  (`Merge <head> into <base>`), which is what CodeQL records as `commit_sha`. It is **not**
  `pull_request.head.sha` and not the PR's `merge_commit_sha`.
- "Complete" means every **category** the repo produces (`/language:python`,
  `/language:javascript-typescript` — one workflow, two matrix legs) has landed for our commit.
  That set is **pinned** via `--required-categories` in the workflow rather than self-calibrated,
  so it can change in a single PR; see "Changing the category set" below. **A partial upload is a partial diff, not a fresh one**: the categories
  land seconds to a minute apart, and comparing a head with two of them against a base with three
  hides every alert in the missing one — the same false green, one step further in. This is not
  theoretical: on PR #913's own gate run the wait cleared at `07:10:36` with javascript and
  python/advanced in, while `/language:python` did not upload until `07:10:54`.
- The required set is **calibrated off the API, never hardcoded** (`expected_categories`): the
  largest category set among the newest 3 commits on the ref that aren't ours. Largest rather than
  newest, because one commit can legitimately be short a category (a matrix leg failed, or its run
  was still uploading when the next push superseded it) and calibrating off that one would let every
  later commit through partial. A PR's **first** push has no earlier commit on its own ref, so the
  set comes from the **base ref**, which runs the same workflows and always has one. If neither has
  an analysis (a repo CodeQL has never scanned), the gate accepts what landed rather than blocking.
  Adding a CodeQL workflow needs no change here; *removing* one costs up to 3 commits of fail-open
  timeouts while the removed category ages out of the lookback.
- `--wait-timeout` defaults to 900s. Before this fix the wait never actually elapsed, so the old
  300s default was never spent; it now has to outlast a real CodeQL run plus queue time.
- `workflow_call` has one identifier for both the ref and the SHA, so it passes **no** `--head-sha`
  and keeps the pre-#904 wait. Passing a ref string as a SHA would match no `commit_sha` at all and
  burn the whole timeout on every call.

This is the same shape as the v0.115.0 release incident in `docs/release-fast-lane.md` ("Step 2
waits on the *run*, not on 'a release PR exists' — those look equivalent and are not").

## Alert identity — a line that moved is not a new alert (issue #1087)

`Alert.key` is `(rule, path, start_line, end_line, message[:200])`, so **line position is part of
identity**. A pre-existing alert whose line shifts because the PR added code *above* it in the same
file therefore reads as new on the head ref — a gate failure on debt the PR did not create, and one
that arrives on any diff big enough to move a line.

Measured on 2026-08-07: PR #1067 added 13 lines above `run_automation.py`'s pre-existing
`py/empty-except` alert (line 4240 on `main` → 4253 on the merge-queue ref). The gate called it new,
the required check failed on every `merge_group` run, GitHub evicted the PR from the merge queue and
the pipeline re-enqueued it next tick. That loop ran ~47h (with the `tick.sh` half tracked in #1082).
The `pull_request` run of the same gate can pass while the `merge_group` run fails — `main` moves
under the PR between the two — so this surfaces in the queue, where there is no PR comment surface
and no human watching.

`compare_alerts` matches head against base **two ways**:

- **Exact** — the `Alert.key` above.
- **Number** — the alert `number`, which is repo-global and stable across refs. GitHub already
  tracks an alert across line movement (SARIF partial fingerprints); the number is that tracking,
  which is exactly what a line key is trying to approximate. A head alert whose number the base
  ref's open set also carries is the same alert, wherever it now sits.

The line key is kept, not replaced: it still catches a genuinely new alert that arrives without a
number. A new alert gets a number the base ref has never seen, so it fails the gate at any line.

**The tolerance is never silent.** Number matching is the only way this comparison can *over*-match,
so `Compared alerts` reports the split — `exact_matched` and `shift_matched` alongside `new_count` —
and `shift_matched_count` is a workflow output. A run where `shift_matched` is large is a run to
look at.

## Fail-open behavior

If the GitHub API is unavailable, the head ref has no CodeQL analysis **for the gated commit** after
the timeout, or the base ref cannot be fetched, the gate logs a loud `::warning` (job summary
included) and exits successfully. A CodeQL outage must not block every PR. It deliberately does
**not** fall back to comparing an older commit's alerts — that is the #904 bug, and it is worse than
not comparing at all.

## Notes

- The gate intentionally does **not** run CodeQL from scratch; it reuses the SARIF uploaded by
  `.github/workflows/codeql-analysis.yml`.

### Changing the category set

`expected_categories()` calibrates off the largest set among the newest 3 commits, which is right
while the set is stable and a trap the moment it SHRINKS: a removed category keeps being demanded
until it ages out, and every run in between waits the full `--wait-timeout` and then fails **open**.
That is the vacuous-required-gate state behind #1168/#1171.

`--required-categories` overrides the calibration. Because the gate checks out the PR's own ref, the
PR that adds or removes a CodeQL workflow carries the matching value, and the change is safe in one
commit. Keep the pinned list in step with `codeql-analysis.yml`'s matrix.

`codeql.yml` (`Advanced Analysis`, `/language:python/advanced`) was removed this way: its
`security-extended` suite is a documented SUBSET of the `security-and-quality` that
`codeql-analysis.yml` runs, over a narrower path set, so it could not find anything the surviving
analysis misses.
- Fixes are pushed with the commit message `chore(codeql): auto-fix mechanical quality alerts`.
- The verification step uses `.github/codeql/codeql-config.yml`, which combines
  `security-extended` and `security-and-quality` on `src/cqc_lem` while ignoring `tests` and `dist`.
  The existing CodeQL workflows are left unchanged.
