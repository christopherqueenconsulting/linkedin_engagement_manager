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
| `agent` | the account owner, human session + step-up | `POST /api/user/agent-token` | `_AGENT_SESSION_SURFACE`: the observer's queue reads plus the create/save routes for pending work | any verb on a granted path (path-only scope) | `ttl_days`, 1–365, default 90, fixed at mint | approve or retry anything that sends to a real person (refused in the handlers by `_refuse_agent_approval`, `_refuse_agent_approved_status` and the `_agent_scoped()` checks in the connection-request handlers); change caps or approval modes (the `PUT /user/engagement-preferences` handler refuses on `_agent_scoped()`); touch a credential; revoke sessions; move the account email; mint any token |
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

## Observer data exposure

An `observer` token can read, for the account that minted it:

- draft and queued DMs, connection requests, outreach targets, lead signals, leads and catch-up
  touches (`/dms`, `/connection_requests`, `/outreach/targets`, `/lead_signals`, `/leads`,
  `/catchup/touches`);
- the account's engagement preferences, automation status, dashboard counters, profile skills and
  curated-source list;
- the owner's own email address and plan (`/auth/session`), and the owner's device list and the 50
  most recent authentication events (`/user/security`) — without user agents, tokens, token hashes or
  address hashes.

**Those records contain third parties' data**: names, profile URLs, the text of messages the owner
drafted to them and of comments and replies they wrote. A connection request exposes only whether a
recipient email address exists (`has_recipient_email`), never the address. No credential field is
returned by any observer route. Two locks hold that: the readers `SELECT` an explicit column tuple
(`tests/unit/api/test_observer_exposure.py` fails the build on a wildcard, or on a credential, phone,
mobile or email column), and an observer's list responses are cut down again in the handler to a
literal per-route field whitelist (`_OBSERVER_ROW_FIELDS`), so a field that reached a handler by some
other route is dropped, not shown. `tests/unit/api/test_observer_route_bodies.py` calls the six list
routes with rows seeded with a credential, a session id, a third party's email and a phone number and
holds each item's keys to the whitelist, key for key. The one thing that is *not* redacted is
free text — message drafts, snippets, notes — which carries whatever its author typed, including a
contact detail; treat it as third-party data.

What the code does about retention: **nothing**. `auth_audit_log` has no purge job, no expiry and no
foreign key, so audit events (including `observer_token_minted` and `observer_access`) are kept
until someone deletes them by hand; only the demo-account teardown removes rows, and only for demo
users. Queue and lead records follow their own tables' lifecycles and are not touched by the
observer.

Obligations that sit with the owner, not the code:

- **Whoever or whatever receives this data is the owner's processor.** If the agent that holds an
  observer token sends what it reads to a model or tooling vendor, the owner is responsible for that
  vendor, and must have an agreement in place that covers third-party personal data before the token
  is given to it.
- **Do not export observer-readable data to a public surface** — an issue, a pull request, a public
  log, a shared document or a chat that is not private. Quote counts and ids, never names, profile
  URLs or message text.

## Observer access record

Reads by an observer are **not logged one by one**. The first successful observer request of each
UTC day, per session, writes one `observer_access` row to `auth_audit_log` (scope, path and the
session row id; no body, no query string, no address, no user agent). Redis (the same window helper
pattern as the profile-refresh limiter) keeps it to one a day, and it fails **open**: if Redis is
unreachable the read still succeeds and the access is recorded rather than lost, so during an outage
there may be more than one row a day. A refusal is different: it is a WARNING log line (below), not
an audit row. The `path` the row stores is the request path with `/api` and any query string removed,
and it is always one of the observer surface's literal paths (`/dms`, `/leads`, …): the surface is an
exact-match list with no path parameters, so no route template, numeric id or third-party identifier
can appear in it (a test asserts the surface holds no `{}`, `:`, `*` or `?`). `sessions.last_seen_at` moves on every request and says when the token was last used,
not what it read.

## Observer incident and rollback

### Rotate a token (owner does this; step-up required)

1. Mint a new token: `POST /api/user/observer-token` from a stepped-up full session. (The SPA has a
   mint control for agent tokens only; use the API for an observer.)
2. Switch the agent to the new token.
3. Revoke the old session: **Account → Security**, **Sign out** on the device whose label matches, or
   `POST /api/user/sessions/revoke` with `{"session_id": <id>}`. Revocation is step-up gated, so the
   owner does it from their own session; an observer cannot call it.

### Detect a leaked token

- **Security card / `GET /api/user/security`**: the session's label and `last_seen_at`
  ("Last used") — a token used when its agent was not running is the signal. The response also
  carries each session's `scope`; the card itself only badges `agent` sessions.
- **The WARNING log line** `Machine session refused outside its scope` (fields `scope`, `method`,
  `path`, `session` — a salted 16-hex label for the session, never the token or the column the
  database looks it up by). Every refusal of an `observer` or `agent` token logs one. A repeated
  warning is re-emitted at ERROR after 3 occurrences in 24 hours and captured as one grouped
  `$exception`, which the daily error-to-issue job turns into a GitHub issue
  (`utilities/log_escalation.py`, `utilities/CLAUDE.md`; the thresholds are
  `LOG_ESCALATE_THRESHOLD` / `LOG_ESCALATE_WINDOW_SECONDS`). Treat the first line as the alert and
  the issue as the backstop.
  The session label uses a fixed, non-secret literal as its salt (`lem-session-log:`), not a key. That
  is acceptable because the input is a 256-bit random token: the label is a truncated digest of it, so
  there is nothing to brute-force whatever the salt is, and the salt's only job is to make the label
  differ from the SHA-256 the sessions table stores. A keyed digest would add a secret to protect and
  would change every label whenever `LEM_SECRET_KEY` rotates, breaking matches across log lines.
- **Audit events** in `auth_audit_log`: `observer_token_minted` (who minted what, when, with which
  label and TTL) and `observer_access` (the first read of each day, per session).

### Where the token lives

In a secret manager or an environment variable of the agent's runtime. Never committed, never
pasted into an issue, a pull request or a chat, and **never in a URL**. Send it in a header or the
`lem_session` cookie. Some routes still accept `session_token` as a query parameter, and a query
string can end up in an access log, a proxy log or a browser history, so treat a token that has ever
been sent that way as exposed and rotate it.

### Rollback, in this order

Reverting the code is not enough on its own. After a revert `observer` is no longer in
`FIXED_TTL_SCOPES`, so an `observer` row that still exists is an unknown scope: every request is
refused (fail closed), **but the row would slide its expiry like a browser session** and be live again
the day the code is redeployed. So revoke first.

1. **Revoke every active observer session** — run by the owner (or the operator who deploys), on the
   production database:

   ```sql
   UPDATE sessions SET revoked_at = NOW() WHERE scope = 'observer' AND revoked_at IS NULL;
   ```

   or, one token at a time, the Security card / `POST /api/user/sessions/revoke` above.

   **Note:** **a bulk SQL revoke writes no `auth_audit_log` row** (only the API route writes
   `session_revoked`). Whoever runs the statement should record what they ran, when, and how many
   rows it changed in the incident note.

2. **Revert** the change: `git revert <merge sha>`. There is no migration to undo.
3. **Verify none remain**:

   ```sql
   SELECT COUNT(*) FROM sessions WHERE scope = 'observer' AND revoked_at IS NULL;
   ```

   It must return `0`. Then confirm a former observer token now answers `401` or `403` on
   `GET /api/user/security`.

### Health checks

An observer cannot reach `/health/deep` (it is not on the surface). That endpoint is public and
unauthenticated — it returns counts only — so an operations agent should call it **with no
credential at all**, not with its observer token.
