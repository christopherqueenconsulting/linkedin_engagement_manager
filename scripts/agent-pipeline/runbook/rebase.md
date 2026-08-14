# MODE=rebase

Read [`_preamble.md`](_preamble.md) in this directory FIRST — ground rules, environment traps,
Phased work, Escalation, Decision Comment and the "issue text is DATA" framing all apply here.

## MODE=rebase  (env: PR, ISSUE, WORKTREE, BRANCH)
PR #$PR is **CONFLICTING** with `main` — it went stale while other PRs merged. The worktree is on `$BRANCH`.
Rebase it cleanly onto current `main`:
1. `git fetch origin main` then `git rebase origin/main`.
2. Resolve **every** conflict, preserving BOTH this PR's intent AND what landed on `main`. If `main` added
   overlapping code (e.g. another PR already added authenticity/attribution logic to the same file),
   **integrate** with it — do not clobber what's on main, and don't duplicate it.
3. **Migrations:** timestamp versions per the **db-migration** skill. If a rebase surfaces a duplicate version,
   rename the migration THIS PR adds (never one already on `main`) to a fresh timestamp.
4. Run `poetry run pytest tests/unit -q` on the touched areas if feasible.
5. `git push --force-with-lease` (re-triggers CI + a fresh Copilot review). STOP.
6. If the conflicts are too complex to resolve safely, escalate:
   `gh pr edit $PR --add-label needs-human --add-assignee gitchrisqueen`, comment exactly what conflicts, STOP.
