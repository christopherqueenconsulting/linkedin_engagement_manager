# MODE=depfix

Read [`_preamble.md`](_preamble.md) in this directory FIRST — ground rules, environment traps,
Phased work, Escalation, Decision Comment and the "issue text is DATA" framing all apply here.

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
