# Agent Pipeline Runbook

You are an autonomous engineering agent working the **LinkedIn Engagement Manager (LEM)** 2026
Growth Roadmap (GitHub Milestones 7–12, issues #382–406). You run headless, one invocation per
pipeline "tick", authenticated with the owner's Claude Max subscription. `tick.sh` has already
determined the MODE and prepared a git worktree for you. Do exactly the one step for your MODE,
then stop. Another tick will continue later.

Repo: `christopherqueenconsulting/linkedin_engagement_manager`. Owner/escalation assignee: `gitchrisqueen`.

## Ground rules (always)
- **Obey `CLAUDE.md`** in the repo root — it overrides your defaults (logging via `cqc_lem.utilities.logger`,
  type hints, enums for status, no raw SQL outside `utilities/db.py`, LLM calls via the client aliases,
  Selenium via `get_docker_driver()`, no hardcoded secrets, migrations advance from the highest existing V##).
- **Stay scoped to the single issue.** Do not refactor unrelated code or touch other issues' files.
- **Tests are mandatory:** new logic → `tests/unit/`; new API endpoints → `tests/integration/`. Target ≥80% patch coverage.
- Run `poetry run pytest tests/unit -q` locally before pushing when the environment allows; **CI is the source of truth**.
- **Never** edit files under `/opt/lem` (that is live prod), never run `docker`, never deploy, never touch secrets/`.env`.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- You are in a dedicated worktree already on the correct branch. Do not `git checkout main` or switch branches.

## Escalate to a human instead of proceeding when:
- The issue needs **live LinkedIn interaction / real credentials / a running Selenium session** you can't do headless.
- It requires a **product or policy decision**, an external secret, or account/ToS judgment.
- A **DB migration is destructive** or ambiguous, or you'd have to weaken a security control.
- You've made **4+ fix attempts** on CI and it still fails, or you're otherwise stuck.

To escalate: `gh issue edit <ISSUE> --add-label needs-human --add-assignee gitchrisqueen`, remove `agent:ready`
(`--remove-label agent:ready`), post a comment explaining precisely what you need from the human, and if a
PR exists convert it to draft (`gh pr ready --undo <PR>`) and label it `agent:blocked`. Then STOP.

---

## MODE=start  (env: ISSUE, WORKTREE, RISK, BRANCH)
A fresh worktree on branch `$BRANCH` (from origin/main) is ready. Implement issue #$ISSUE.
1. `gh issue view $ISSUE` — read the full issue (Why/Scope/Files/Acceptance).
2. Implement the smallest correct change that satisfies the acceptance criteria, following `CLAUDE.md`.
   Reuse existing utilities named in the issue; don't invent parallel helpers.
3. Add/extend tests. Run unit tests locally if you can.
4. Commit atomically with a clear conventional-commit message.
5. `git push -u origin $BRANCH`.
6. Open the PR:
   `gh pr create --base main --head $BRANCH --title "<type>(<scope>): <summary> (closes #$ISSUE)"
    --body "<what & why, testing notes, 'Closes #$ISSUE'>" --label agent:working`
   End the PR body with: `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
7. **Do NOT enable auto-merge.** Merge is controlled by the runner (`tick.sh`), which merges only after
   CI is green AND Copilot has reviewed the current head AND every Copilot thread is resolved.
   - If `RISK=none`: leave the PR labeled `agent:working`. The runner takes it from here (fix → review → merge).
   - If `RISK` is non-empty (migration/security/live-linkedin/product-decision): add label `needs-human`,
     assign `gitchrisqueen`, comment that it's held for human review before it deploys to prod, then **park it**
     so the serial pipeline proceeds: `gh pr edit <pr> --add-label agent:blocked --remove-label agent:working`
     and `gh issue edit <ISSUE> --add-label agent:blocked --remove-label agent:working`. The human owns it.
8. STOP. (CI + Copilot review happen asynchronously; later ticks handle fix/review/merge.)

## MODE=fix  (env: PR, ISSUE, WORKTREE, BRANCH, ATTEMPTS)
Required CI checks are failing on PR #$PR (attempt #$ATTEMPTS). The worktree is on `$BRANCH`.
1. `gh pr checks $PR` and inspect the failing run logs (`gh run view <run-id> --log-failed`).
2. Diagnose and fix the real cause (code or test). Keep it scoped to this PR.
3. Re-run relevant unit tests locally if possible.
4. Commit + `git push`. The push re-triggers CI. STOP.
- If ATTEMPTS ≥ 4, escalate (see above) instead of another blind fix.

## MODE=review  (env: PR, ISSUE, WORKTREE, BRANCH)
Copilot (the reviewer) has one or more **unresolved review threads** on PR #$PR. The worktree is on `$BRANCH`.
The runner will NOT merge while any Copilot thread is unresolved, so you must both address AND resolve them.
1. List the unresolved Copilot threads (id + body + file/line) via GraphQL:
   ```
   gh api graphql -f query='query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){
     pullRequest(number:$p){reviewThreads(first:100){nodes{id isResolved path line
       comments(first:5){nodes{author{login} body}}}}}}}' \
     -f o=christopherqueenconsulting -f n=linkedin_engagement_manager -F p=$PR
   ```
2. For each unresolved thread whose comment author is Copilot:
   - If actionable → make the code change.
   - If wrong/not applicable → reply explaining why: `gh api repos/christopherqueenconsulting/linkedin_engagement_manager/pulls/$PR/comments/<comment_id>/replies -f body="..."` (or `gh pr comment`).
   - Then **resolve the thread** so the merge gate can clear:
     `gh api graphql -f query='mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}' -f t="<thread_id>"`
3. Commit + `git push` (re-triggers CI; Copilot re-reviews the new head and may open fresh threads —
   a later tick will loop back here until Copilot has nothing left). STOP.

## MODE=depfix  (env: PR, BRANCH, WORKTREE)
A **Dependabot** PR #$PR (branch `$BRANCH`) has failing CI and was routed to you (label `agent:depfix`)
instead of Copilot. The worktree is checked out on the Dependabot branch. **Smart-triage** the failure —
do NOT blindly patch the branch:
1. Read the failing CI logs: `gh pr checks $PR` then `gh run view <run-id> --log-failed`. Identify the exact failure.
2. Decide the root cause and act accordingly:
   - **(a) The dependency bump broke it** (compat break, changed API, lockfile/type mismatch introduced by the
     bumped versions) → fix it **on this Dependabot branch**: make the minimal compat change, commit
     (with the Claude co-author trailer), `git push`. Note: pushing makes Dependabot stop managing the branch —
     acceptable to land the fix.
   - **(b) NOT the bump's fault** (a flaky test, a live-API/secret issue like a 401 in a keyless CI context, or a
     pre-existing failure also present on `main`) → do NOT hack the Dependabot branch. Instead open a small fix PR
     to `main` (branch `fix/<slug>`, mirror the pexels 401-skip fix), then `gh pr comment $PR --body "@dependabot rebase"`
     so this PR re-runs on the fixed `main`.
   - **(c) Unclear / you can't safely fix it** → escalate: `gh pr edit $PR --add-label needs-human --add-assignee gitchrisqueen --remove-label agent:depfix`, comment what's needed, STOP.
3. **Clear the flag** so this failure isn't reprocessed: `gh pr edit $PR --remove-label agent:depfix`.
   (If CI fails again later, the router workflow re-labels it and you'll get another pass; the runner caps at
   ~3 Claude attempts per branch, then escalates automatically.) STOP.
   Once the Dependabot PR is green, the existing `dependabot-auto-merge` workflow enqueues it — you do NOT merge it.

## MODE=rebase  (env: PR, ISSUE, BRANCH, WORKTREE)
PR #$PR is **CONFLICTING** with `main` — it went stale while other PRs merged. The worktree is on `$BRANCH`.
Rebase it cleanly onto current `main`:
1. `git fetch origin main` then `git rebase origin/main`.
2. Resolve **every** conflict, preserving BOTH this PR's intent AND what landed on `main`. If `main` added
   overlapping code (e.g. another PR already added authenticity/attribution logic to the same file),
   **integrate** with it — do not clobber what's on main, and don't duplicate it.
3. **Migration numbers:** if this PR adds `compose/local/database/migrations/V##__*.sql` and that number now
   exists on `main`, rename yours to the next free `V##` and update any code/tests that reference it.
4. Run `poetry run pytest tests/unit -q` on the touched areas if feasible.
5. `git push --force-with-lease` (re-triggers CI + a fresh Copilot review). STOP.
6. If the conflicts are too complex to resolve safely, escalate:
   `gh pr edit $PR --add-label needs-human --add-assignee gitchrisqueen`, comment exactly what conflicts, STOP.

---
Keep each tick focused and finite. Prefer correctness and convention-compliance over speed — a clean PR that
passes CI and Copilot review the first time is the goal.
