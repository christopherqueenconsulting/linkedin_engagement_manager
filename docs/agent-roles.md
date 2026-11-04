# Machine roles — what each session scope may do

LEM has one human seat (`full`) and a small set of **machine seats**: sessions minted once by the
account owner, held by a non-browser caller, and narrowed to a named surface. Every machine seat is
minted by a human in the SPA behind step-up, has a **fixed expiry that is never slid**, is listed and
revocable on the Security card, and **cannot mint another token**. The mechanism and the reasoning are
in [`identity-and-sessions.md`](identity-and-sessions.md); this page is the lookup table. Every
role is enforced in ONE place, `get_session_user_id()` (CLAUDE.md: *Session scopes are SURFACES*), so
a route that imports around it is a bug.

| Role (`sessions.scope`) | Who mints it | Mint endpoint | Surface | Verbs | TTL | Cannot do |
|---|---|---|---|---|---|---|
| `observer` | the account owner, human session + step-up | `POST /api/user/observer-token` | `_OBSERVER_SESSION_SURFACE`: review-queue reads, preferences, automation status, dashboard stats, curated-source list, `/auth/session`, `/app-info`, `/flags`, `/user/security` | `GET` `HEAD` `OPTIONS` only, on every path | `ttl_days`, 1–365, default 90, fixed at mint | create, edit, delete or approve anything; read or write a credential; change caps or approval modes; revoke sessions; move the account email; mint any token |
| `agent` | the account owner, human session + step-up | `POST /api/user/agent-token` | `_AGENT_SESSION_SURFACE`: the observer's queue reads plus the create/save routes for pending work | any verb on a granted path (path-only scope) | `ttl_days`, 1–365, default 90, fixed at mint | approve or retry anything that sends to a real person (three server-side guards); change caps or approval modes (`PUT /user/engagement-preferences` is refused); touch a credential; revoke sessions; move the account email; mint any token |
| `extension` | the account owner, human session + step-up | `POST /api/user/extension-token` | `/user/linkedin-cookie` only | the verbs that route serves | idle window (slides) | everything else |
| `enroll` | the sign-in flow, when a strong factor is required and none is enrolled | — | the enrolment ceremonies only; promoted to `full` on enrolment | the verbs those routes serve | idle window (slides) | everything else |

## The hard rules every machine role keeps

No machine role may approve a send to a real person, touch a credential path
(`/user/linkedin-cookie`, `/user/linkedin-password`, `/user/passkeys/*`, `/user/totp/*`,
`/user/recovery-codes/*`), change the account mover (`/user/email/change/*`), revoke sessions, mint
tokens, or change caps or approval modes. An unknown scope or an unknown path fails **closed**.

These are enforced by tests over the live route table, not by this page:
`tests/unit/api/test_role_surface_invariants.py` fails, with the offending routes listed, if any role
can reach a member of `FORBIDDEN_ROLE_ROUTES` — and the next role added to the table is held to the
same list.

## Revoking one

Every machine seat is an ordinary session row. Revoke it from **Account → Security**; it stops that
caller and signs nobody out. A token that is never revoked still expires on the day it was minted to.
