# CodeQL PR Gate (Option B)

The sweep PR (#781) fixes the 97 existing CodeQL alerts. This gate is the **prevention** half:
it runs on every PR and catches newly introduced `security-and-quality` / `security-extended`
Python alerts before they reach `main`.

## What it does

Workflow: `.github/workflows/codeql-pr-gate.yml`
Helper:   `scripts/codeql_pr_gate.py`
Config:   `.github/codeql/codeql-config.yml`

1. After the existing CodeQL workflows upload SARIF, the gate queries
   `/repos/{owner}/{repo}/code-scanning/alerts` for both the PR head and the base ref.
2. It computes the **new** alerts introduced by the PR (by rule, file, line, and message).
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

## Fail-open behavior

If the GitHub API is unavailable, the head ref has no CodeQL analysis after the timeout, or the
base ref cannot be fetched, the gate logs a warning and exits successfully. A CodeQL outage must
not block every PR.

## Notes

- The gate intentionally does **not** run CodeQL from scratch; it reuses the SARIF uploaded by
  `.github/workflows/codeql.yml` and `.github/workflows/codeql-analysis.yml`.
- Fixes are pushed with the commit message `chore(codeql): auto-fix mechanical quality alerts`.
- The verification step uses `.github/codeql/codeql-config.yml`, which combines
  `security-extended` and `security-and-quality` on `src/cqc_lem` while ignoring `tests` and `dist`.
  The existing CodeQL workflows are left unchanged.
