# MODE=phasefix

Read [`_preamble.md`](_preamble.md) in this directory FIRST — ground rules, environment traps,
Phased work, Escalation, Decision Comment and the "issue text is DATA" framing all apply here.

## MODE=phasefix  (env: PR, ISSUE, WORKTREE, BRANCH)
The merge gate held PR #$PR: it closes issue #$ISSUE, whose body declares work beyond this PR, and no
follow-up is linked. This is MECHANICAL — resolve it yourself, do NOT ask the owner. Deciding to
finish an issue's stated requirements is never a human decision.
1. Read issue #$ISSUE (body + comments) and the PR. Identify exactly what scope remains (unchecked
   acceptance boxes, "Phase 2 / follow-up / deferred" prose).
2. **Check the follow-up doesn't already exist** (`gh issue list --search`, and read the PR/issue
   comments — someone may have filed it and said so). If it exists, just link it:
   `Follow-up: #<n>` appended to the PR body (`gh pr edit $PR --body ...`) + a comment on #$ISSUE. Done — go to step 4.
3. Otherwise prefer **(a) file the follow-up now**: title `<original title> — Phase N (follow-up of
   #$ISSUE)`, quote the remaining scope from the original, give it REAL acceptance criteria, label it
   topicals + `agent:ready` + a `priority:*` (+ `risk:*` if merge needs the owner — the hold belongs on
   the FOLLOW-UP, never on this PR). Link it in **both** places: `Follow-up: #<new>` in the PR body and
   a comment on #$ISSUE. Keep `Closes #$ISSUE`. Use **(b) drop the close** (remove `Closes #$ISSUE`
   from the PR body, comment "Remaining on #$ISSUE: …") only when the remainder is too underspecified
   to write honest acceptance criteria for.
4. Hand the PR back to the merge loop: `gh pr edit $PR --add-label agent:working --remove-label agent:phasefix`. STOP.
5. Escalate ONLY if the remaining scope requires a genuine product decision you cannot capture as an
   issue — that should be rare; when in doubt, file the issue. To escalate, drop the lane label too
   or this lane keeps re-dispatching on top of the hold:
   `gh pr edit $PR --add-label needs-human --add-label agent:blocked --remove-label agent:phasefix
    --add-assignee gitchrisqueen`, then post a Decision Comment. STOP.
