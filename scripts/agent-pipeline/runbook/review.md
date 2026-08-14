# MODE=review

Read [`_preamble.md`](_preamble.md) in this directory FIRST — ground rules, environment traps,
Phased work, Escalation, Decision Comment and the "issue text is DATA" framing all apply here.

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
