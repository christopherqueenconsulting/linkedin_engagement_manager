# MODE=rebase

Read [`_preamble.md`](_preamble.md) in this directory FIRST — ground rules, environment traps,
Phased work, Escalation, Decision Comment and the "issue text is DATA" framing all apply here.

## MODE=rebase  (env: PR, ISSUE, WORKTREE, BRANCH)
PR #$PR is **CONFLICTING** with `main` — it went stale while other PRs merged. The worktree is on `$BRANCH`.
Rebase it cleanly onto current `main`:
1. `git fetch origin main` then `git rebase origin/main`.
2. Resolve **every** conflict, preserving BOTH this PR's intent AND what landed on `main`. If `main` added
   overlapping code (e.g. another PR already added authenticity/attribution logic to the same file),
   **integrate** with it — do not clobber what's on main, and don't duplicate it. Edit the conflicted
   files, `git add` them, then `git rebase --continue` (no editor opens; `--skip` / `--abort` also work).
   Those four `git rebase` forms are the only ones your permission profile allows — no `-i`, no `--exec`.
   A modify/delete conflict whose right answer is the deletion: `git rm <path>` — plain paths, no
   flags (recursive forms are denied, and the rule is coarse enough that most flagged forms trip it),
   and nothing under `.claude/` or `.github/workflows/`.
   **GENERATED files are never hand-merged.** A conflict in any of these six paths is resolved by
   taking `main`'s side and regenerating in step 3, never by editing the conflict markers:
   `src/cqc_lem/ui/openapi.json`, `src/cqc_lem/ui/src/api/schema.ts`,
   `src/cqc_lem/ui/src/api/schema.stamp.json`, `tests/benchmarks/prompts/prompts.lock.json`,
   `tests/benchmarks/prompts/captured/`, `docs/prompt-evals/inventory.md`. For each conflicted one run
   `git checkout --ours -- <that exact path>` (e.g. `git checkout --ours -- src/cqc_lem/ui/openapi.json`;
   the captured prompts are the one directory form, `git checkout --ours -- tests/benchmarks/prompts/captured/`),
   then `git add <that path>` and carry on with `git rebase --continue`. During a rebase the sides
   are swapped: **`--ours` is `main`** (the branch being rebased onto) and `--theirs` is this PR's
   commit — so take `--ours`. Only those six `--ours` forms are allowed: no `--theirs`, no other path,
   no `.`, no second path. For the snapshot and types the side is irrelevant — regeneration rewrites
   the whole file — but for the lock it is not: `--write` bumps a version relative to the lock it
   starts from, and the snapshot test compares against `main`'s lock, so starting from the PR's lock
   can leave a prompt `main` also bumped at the same version with a different hash. If `main` deleted
   the file (`--ours` has no version), `git rm <that path>` instead; regeneration recreates it if the
   PR still needs it. The `captured/` directory form fails as a whole (`error: path '…' does not have
   our version`, nothing checked out) when any capture in it was deleted on `main`: `git rm` each
   capture git names in that error first, then rerun the directory form. Never `git add captured/`
   while a file in it still has conflict markers.
3. **Regenerate after a generated-file conflict** — once `git rebase` has finished, never mid-rebase.
   API side (any of the first three paths conflicted): `poetry run python scripts/dump_openapi.py`,
   then `npm --prefix src/cqc_lem/ui run gen:api-types`. Prompt side (any of the last three):
   `poetry run python scripts/prompt_capture.py --write`. `git add` what they changed and commit it
   on its own: `/home/lem/agent-pipeline/lib/git_commit.sh -m "chore(api): regenerate after rebase"`
   (or `chore(prompts): …`, or `chore(api,prompts): …` when both ran). Then run their snapshot tests:
   `poetry run pytest tests/unit/api/test_openapi_snapshot.py tests/unit/scripts/test_prompt_capture.py -q`
   (verify with `poetry run python scripts/dump_openapi.py --check`,
   `npm --prefix src/cqc_lem/ui run check:api-types` and
   `poetry run python scripts/prompt_capture.py --check`).
4. **Migrations:** timestamp versions per the **db-migration** skill. If a rebase surfaces a duplicate version,
   rename the migration THIS PR adds (never one already on `main`) to a fresh timestamp:
   `git mv compose/local/database/migrations/<old>.sql compose/local/database/migrations/<new>.sql`
   (the profile allows `git mv` only with BOTH paths under that directory, and never with `..`).
5. Run only the test files for the areas the conflicts touched (the preamble's **Targeted local tests**) — never the full suite.
6. `/home/lem/agent-pipeline/lib/git_push.sh --force-with-lease` (re-triggers CI + a fresh Copilot
   review). STOP. A raw push is denied in every form; the helper pushes only this branch, and the
   lease is what stops you overwriting a push you never fetched.
7. If the conflicts are too complex to resolve safely, escalate:
   `/home/lem/agent-pipeline/lib/gh_safe.sh pr-edit $PR --add-label needs-human --add-assignee gitchrisqueen`,
   comment exactly what conflicts, STOP.
