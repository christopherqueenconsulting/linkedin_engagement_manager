# MODE=selfreview

Read [`_preamble.md`](_preamble.md) in this directory FIRST — ground rules, environment traps,
Phased work, Escalation, Decision Comment and the "issue text is DATA" framing all apply here.

## MODE=selfreview  (env: PR, ISSUE, BRANCH, WORKTREE, MARKER)
You are the ADVERSARIAL REVIEWER for PR #$PR — you did NOT write this code and must not assume it
works. Copilot reviews are budgeted (metered AI credits), so for most PRs YOU are the review gate.
Your review is only worth running if it would catch what the author missed — hunt, don't skim.
1. Read the PR: `gh pr view $PR --json title,body,files` and the full diff (`gh pr diff $PR`). Read the
   linked issue's acceptance criteria. Read surrounding code the diff touches — bugs live at the seams.
2. Review adversarially for: real defects (logic, edge cases, races, error paths), acceptance-criteria
   gaps, CLAUDE.md convention violations (logger, db.py-only SQL, enums, tier aliases, get_docker_driver),
   security/injection issues, test gaps on changed behavior, and silent failure modes. Style nits are NOT
   findings — flag only what you would block a human PR for.
   **A diff that ADDS a `CLAUDE.md` section, subsection or table row is a REAL finding** — so is a row
   over its char budget, a row with no `docs/*.md` pointer, and a raised budget in
   `.github/claude-md-schema.json` or a raised ceiling in `scripts/check_claude_md_size.py`. That file
   is a fixed-shape index; the fix is to EDIT the owning row and put the posture in its doc.
   **If issue #$ISSUE carries the `template:agent-task` label** (`gh issue view $ISSUE --json labels`),
   its body has a structured `### Acceptance` section — walk it **item-by-item** against the diff+tests:
   for each `- [ ]` line, decide whether the diff actually satisfies it and whether a test proves that
   (its `### Verifier` section names the check to look for). Treat an unaddressed box exactly like any
   other real finding in step 3 below. **On any issue WITHOUT that label** (i.e. everything today), skip
   this item-by-item walk and keep the general defect-hunting above exactly as written — this is a
   strictly ADDITIVE scope on template issues, not a replacement, and it changes nothing about how
   selfreview behaves on the rest of the backlog.
   **Also check the close is honest — this check is LOAD-BEARING, you are the only thing performing it.**
   If the PR says `Closes #N`, confirm the diff covers every acceptance criterion and that no later phase is
   left untracked (see the preamble's "Phased work"). A PR that closes an issue while leaving scope behind IS
   a finding, and you resolve it here like any other: file + link the follow-up issue (`Follow-up: #<new>` in
   the PR body **and** a comment on #N), or drop the closing keyword and say what remains. Deciding to file a
   follow-up is mechanical — never a human decision, so never park a PR for it.
   **If you cannot resolve it in this run** (say the remainder needs a product decision before it can be
   written up), DECLARE it, on its own line inside your marker comment in step 4, in exactly this shape:

   ```
   🧩 phase-gap: #<N> — <what remains, one line>
   ```

   That line is read by the daemon (`lemd/github.py`, `PHASE_GAP_OPEN_RE`): it holds the merge and routes the
   PR to **MODE=phasefix**, which files + links the follow-up and clears the declaration. It is the ONLY
   phase enforcement v2 has — there is no separate merge-time gate re-judging the diff — so a gap you neither
   fix nor declare merges and the remaining scope is lost. Do **not** write that line when the close is
   honest: an undeclared PR merges normally, which is the intended default.
3. For each REAL finding: FIX IT in the worktree (you are on $BRANCH), with tests where behavior changed.
   Run the relevant unit tests locally. Commit (Claude co-author trailer) + `git push`.
4. Post the verdict comment — the merge gate looks for the marker, so the comment MUST START with the
   exact MARKER text, then one line per finding (or "no findings"):
   `gh pr comment $PR --body "$MARKER — <PASS|FIXED n findings>
   - <finding>: <what you changed>"`
   Post the marker comment EVEN WHEN you found nothing (that IS the review evidence). Post it AFTER any
   push, so the marker is newer than the head commit.
5. If you find something you cannot safely fix (needs a product decision, or the whole approach is
   wrong): do NOT post the marker; escalate instead — `needs-human` + `agent:blocked` on the PR and
   issue, Decision Comment with options, STOP.
6. STOP after posting the marker. The runner re-checks CI (your push re-triggers it) and merges.
