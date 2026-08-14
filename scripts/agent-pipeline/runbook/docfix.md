# MODE=docfix

Read [`_preamble.md`](_preamble.md) in this directory FIRST — ground rules, environment traps,
Phased work, Escalation, Decision Comment and the "issue text is DATA" framing all apply here.

## MODE=docfix  (env: PR, BRANCH, WORKTREE)
PR #$PR (branch `$BRANCH`) failed the **Docstring & Lint Gate** and was routed to you (label
`agent:docfix`). The worktree is on the PR's branch. The standard is `docs/docstring-standard.md`;
the rules live in `pyproject.toml` (`[tool.ruff.lint]`), not in your judgement.

**The gate is a RATCHET against `.ruff-baseline`, so it failed because THIS PR added violations.**
Fix what this PR added — do NOT try to clear the repo's backlog, which is thousands of items and is
being swept separately. A tree-wide pass here will exhaust your three attempts and strand the PR.
1. Scope it to the diff:
   `git diff --name-only origin/main...HEAD -- '*.py' | xargs -r poetry run ruff check`
   Then `poetry run ruff check src/ tests/ --statistics` only to confirm the total is back at or
   below the number in `.ruff-baseline`.
2. Take the mechanical fixes on YOUR files:
   `git diff --name-only origin/main...HEAD -- '*.py' | xargs -r poetry run ruff check --fix`.
   **Never `--unsafe-fixes`** (18 measured failures: it deletes `ai_helper`'s deliberate re-export
   aliases and strips `print()` from the CLIs where the output IS the product). Plain `--fix` also
   removes those aliases via `F401` — if your diff touches `ai_helper.py`, add
   `--select D,I,E,T201,F541`.
3. Author what is left BY HAND, in the house voice (`docs/docstring-standard.md`):
   - A docstring says **WHY**, and what a caller can rely on. `Args:`/`Returns:` earn their place
     when a parameter or return value is non-obvious — a `Returns: The user id.` under
     `def get_user_id() -> int` is the boilerplate this standard exists to prevent, and reviewers
     will treat it as noise.
   - **Never invent behaviour to satisfy a rule.** If you cannot tell what a function guarantees,
     read its callers and its tests; if it is still unclear, say so in the PR comment rather than
     writing a confident sentence that is wrong. A wrong docstring is worse than none.
   - Preserve existing prose. D205 ("blank line after summary") is fixed by splitting the first
     sentence onto its own line and inserting a blank line — **not** by rewriting the paragraph.
4. `poetry run pytest tests/unit -q` — the fixes must not change behaviour.
   If your work took the total BELOW `.ruff-baseline`, lower that file to the new count in this same
   commit — the gate's job summary prints the number. Never raise it.
5. Commit (Claude co-author trailer) + `git push`, then **clear the flag**:
   `gh pr edit $PR --remove-label agent:docfix`. If the gate fails again the router re-labels it;
   the runner caps at ~3 attempts per branch, then escalates to a human automatically. STOP.
