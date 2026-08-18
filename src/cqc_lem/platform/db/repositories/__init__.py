"""Per-aggregate SQL modules split out of `cqc_lem.utilities.db` (issues #1154, #1614).

One module per aggregate, owning every statement against that aggregate's tables. Which aggregate
owns which table was read off the migrations, not chosen: `users`, `posts`, `auth`, `outreach`,
`engagement`, `newsletter`, `feedback`, `avatar`, `groups`, `billing`. `utilities/db.py` re-exports
all of it and remains the name ~2,400 call sites import from.

`dashboard.py` is the one module that is not an aggregate. It holds the reads that span several and
own no table of their own; see its docstring for why that is not `shared.py`.

**Where a function that touches two aggregates went, and why.** These are the eight the mechanical
splitter refused to place — it counted the tables a statement names and declined to break a tie
rather than resolve one by coin flip. Each was decided on the same question: which aggregate's row
is the SUBJECT of the statement, as opposed to joined for context.

| function | home | the subject |
|---|---|---|
| `create_session` | auth | writes a `sessions` row; `users` resolves the account it points at |
| `get_feedback_list` | feedback | every row returned is feedback; `users` is the reporter join |
| `grant_affiliate_trial_days` | billing | the reward ledger; `users.trial_end` is the effect |
| `revoke_affiliate_enrollment_bonus` | billing | same ledger, same effect |
| `count_artifact_cta_deliveries` | outreach | counts DELIVERIES (`scheduled_dms`); `posts` is the CTA |
| `count_existing_double_sent_catchups` | outreach | `catchup_touches`; `logs` is the evidence |
| `list_existing_double_sent_catchups` | outreach | same |
| `get_planned_tasks` | dashboard | owns no table at all — three aggregates, one card |

**A wrapper that runs no SQL follows the repository it delegates to**, which is also what keeps it
from needing a cross-module import: `is_premium_subscriber` reads a users row so it sits in `users`
next to `max_catchup_touches_allowed`, its only in-tree caller, even though the QUESTION is a
billing one; `count_dms_sent_today` counts `logs` rows so it sits in `engagement`, even though the
subject is a DM.

**Patch targets.** A function here reads its collaborators out of THIS module's globals, so
patching `cqc_lem.utilities.db.X` binds an object it never consults and the test passes against
real SQL. Patch the name where it lives.
`tests/unit/platform/db/test_connection_seam.py` derives that hazard set per module — closed over
the intra-module call graph, and matching `patch.object(db, "X")` as well as the path string — and
fails the build on it.
"""
