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
| `operator` | the account owner, human session + step-up | `POST /api/user/operator-token` (internal, sandbox or admin account only) | the agent surface plus `_OPERATOR_MAINTENANCE_SURFACE`: `POST /leads/refresh`, `GET /admin/automation-status`, `POST /admin/automation-pause`, `GET /admin/task-status/{id}` | any verb on a granted path (path-only scope) | `ttl_days`, 1–**90**, default **30**, fixed at mint | everything the agent cannot do, plus: be minted on, or act through, a `standard` non-admin account; approve on a `standard` account; take the passive `auto_approve` default on any account; resume automation; pause for more than 24 hours; call any other admin route |
| `extension` | the account owner, human session + step-up | `POST /api/user/extension-token` | `/user/linkedin-cookie` only | the verbs that route serves | idle window (slides) | everything else |
| `enroll` | the sign-in flow, when a strong factor is required and none is enrolled | — | the enrolment ceremonies only; promoted to `full` on enrolment | the verbs those routes serve | idle window (slides) | everything else |

## The hard rules every machine role keeps

No machine role may approve a send to a real person — **with one exact exception**: an `operator`
token acting on an account whose `users.account_kind` is `internal` or `sandbox` may approve an item
explicitly (`action="approve"` or `"retry"`, or a create with `status="approved"`), and every such
approval is audited as `operator_approval`. An operator on a `standard` account, an `agent` and an
`observer` may not, and **no** seat — the operator included, on any account kind — gets the passive
`auto_approve` default. No machine role may touch a credential path
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

## Operator: who may hold one, data exposure, access record and rollback

**Who may hold an operator token, and why.** An operator token can call `POST /admin/automation-pause`,
and **that pause is global**: it stops Selenium automation for **every user of the deployment**, not
only the token's owner, and **only the owner's `X-Admin-Secret` can lift it** (`automation-resume` is
secret-only on purpose). A token minted on an ordinary customer or owner account would therefore be a
credential that can switch every other account's automation off and that its holder cannot switch
back on. So the mint (`POST /api/user/operator-token`) answers **403 `operator_not_permitted`** unless
the human caller's `users.account_kind` is `internal` or `sandbox`, or the caller is an admin
(`users.is_admin` or `ADMIN_USER_EMAILS`), and it does so **before** asking for a step-up. The same
check runs again on **every use** of the three operator admin routes (`_operator_session`), so a token
minted before the gate existed, or on an account that has since lost its flag, is refused with a 403 at
use time; the owner's secret still works beside it. The pause an operator may request is a finite
0 to 24 hours (a non-finite or out-of-range `hours` is a 422 and nothing is paused); the secret holder is
not capped. `hours=0` is accepted and means a **one-hour** pause (the handler turns a zero into
3600 seconds), for an operator as for the secret holder; this is documented, not changed. The pause state
is recorded with the reason `admin` for both, so the Redis state cannot tell an operator's pause from the
owner's — the `operator_action` and `operator_access` audit rows are what tell them apart. The operator's
TTL is capped at **90 days, default 30** — shorter than the agent's and the
observer's 365 / 90, because this seat can act on every user.

**Task status.** `GET /admin/task-status/{id}` shows an operator a task's *state* (and `result_withheld`),
for any task id the operator holds. The limit: the Celery result backend here does not store which user
a task belongs to (`result_extended` is off), so the route **cannot be scoped to tasks the caller owns**.
The mitigations are that task ids are random UUIDs nobody is shown by another route, and that the
result text is withheld; the state alone (`PENDING`, `SUCCESS`, `FAILURE`) is all that leaks.

**Data.** An operator reads what an agent reads — the review queues, which contain third parties'
names, profile URLs and message text — plus the automation pause state and a job's state. The
observer's per-route field whitelist is observer-only, so an operator's queue responses are the
agent's. No route the operator gained returns a contact detail: `POST /leads/refresh` returns fixed
text, the two automation routes return counters, and `GET /admin/task-status/{id}` withholds the
task's result text from an operator. The owner's obligations in § Observer data exposure apply to
whatever agent or vendor holds an operator token, and the data must not be exported to a public
surface. **Before any non-owner data enters a queue an operator can read, the owner must have recorded
the model or tooling vendor that agent uses and the agreement that covers that data.**

**Access record.** Operator actions that are not reads write one `operator_action` row each and every
approval writes `operator_approval`; reads are not logged one by one. The first **authorised** operator
request of each UTC day, per session, writes one `operator_access` row (scope, path, account kind,
session row id), and it **fails open**: if Redis is unreachable the request is still served and the
access is recorded rather than lost, so during an outage there can be more than one row a day. A
refusal is a WARNING log line (same template and escalation as the observer's), not an audit row.
`label` on the mint is limited to letters, digits, space and `._:-` (120 max).

**Calling the admin routes without a query token.** `session_token` as a query parameter can end up in
an access log, so the cookie form works and is the one to use: send the token as the `lem_session`
cookie (the resolver falls back to it when no `session_token` is given); a state-changing call that
authenticates on the cookie must also send `X-LEM-Client`. An `Authorization` header is **not** a
session credential on these routes. A test calls all three operator admin routes with only the cookie.

**Rotate, detect, store.** As for the observer (§ Observer incident and rollback), with
`operator_token_minted`, `operator_access`, `operator_action` and `operator_approval` as the audit
events to read, and the same rule that a token never goes in a URL. Token rotation is the account
owner's: only a stepped-up human session can mint or revoke one. When operator refusals escalate (3 in
24 hours become one grouped `$exception`) the daily error-to-issue job files a GitHub issue in this
repository; there is no pager. **Owner:** the account owner, with the on-call engineer as the backup.
**Cadence:** once a day after the job runs (`scripts/error_to_issues.sh`, cron `30 8 * * *` on the VPS
host, `docs/error-tracking.md`), search the repository's open issues for the title text
`Machine session refused outside its scope`, and triage the same day. The job files each issue with the
`bug` and `agent:ready` labels, and `agent:ready` lets the agent pipeline pick it up, so the owner
should hold or relabel one that is really a security question. The issue is filed in a **public**
repository: the log line it is built from carries `user_id`, `scope`, `method`, `path` and a salted
session hash, never a token, an address or a name.
The number of active operator tokens per account is **not capped** — the mint will create as many as
are asked for — so count them: `SELECT COUNT(*) FROM sessions WHERE scope = 'operator' AND revoked_at IS NULL AND expires_at > NOW();`.

**An ops agent that gets a 401 or 403** stops and reports. It does not retry, rotate to another token
or fall back to the owner's secret; a 403 `operator_not_permitted` or `session_scope_forbidden` means
the seat is not allowed to do that, and retrying only writes more WARNING lines.

### Operator rollback, in this order

Reverting leaves any minted operator session an unknown scope (every request refused, fail closed),
but it would slide its expiry like a browser session and work again the day the code is redeployed.
So revoke first. **Run by the on-call engineer or the account owner**, never by an operator token.

**0. Reach the production database, and the admin secret.** Everything here runs on the VPS.

- **Access.** `ssh deploy@<vps>` (`docs/DEPLOYMENT.md`; the host is the `VPS_HOST` repo secret). SSH is
  held by whoever has a public key in `/home/deploy/.ssh/authorized_keys`: the account owner, and the CI
  deploy key (`VPS_SSH_KEY`). The repository lists nobody else, so **if you are the on-call engineer
  and you cannot log in, you do not have access: go to the fallback below**. Docker commands need
  `sudo`.
- **Confirm the container name first.** Compose names the MySQL container `${MYSQL_HOST}`
  (`docker-compose.yml`), which is `mysql_db` in `.env.prod.example`:

  ```bash
  sudo docker ps --format '{{.Names}}'        # expect mysql_db in the list
  ```

- **Load the password without printing it.** It is `MYSQL_ROOT_PASSWORD` in `/opt/lem/.env` (owned by
  `deploy:deploy`, mode 600, so read it as `deploy`, without `sudo`). Read that one line, never the whole
  file (`docs/AUTOMATION_COOLDOWN.md`), and hand it to the container by variable **name** so it is not on
  any command line or in `ps`:

  ```bash
  export MYSQL_PWD="$(grep -E '^MYSQL_ROOT_PASSWORD=' /opt/lem/.env | cut -d= -f2-)"   # strip quotes if the value is quoted
  sudo -E docker exec -i -e MYSQL_PWD mysql_db mysql -u root linkedin_manager
  unset MYSQL_PWD                                                                       # when you are done
  ```

  (Sourcing the file with `set -a; . /opt/lem/.env; set +a` also works, but it executes the file as shell
  and exports every secret into your session; the one-line form is safer.) Never paste the value into a
  ticket or the incident note.
- **The admin secret** that `POST /api/admin/automation-pause` and `automation-resume` need is
  `ADMIN_SECRET` in the same `/opt/lem/.env`; it is server-side only and is never issued to a browser
  or to a machine token. **Only the account owner uses it**; the on-call engineer uses it only when the
  owner has handed it over for the incident, and an ops agent never does. To pause without putting the
  value on a command line, write it to a header file and pass `-H @file` (`BASE` is the deployment's
  base URL, shown in `docs/TESTING_ENGAGEMENT_API.md`):

  ```bash
  umask 077; HDR="$(mktemp)"
  grep -E '^ADMIN_SECRET=' /opt/lem/.env | sed 's/^ADMIN_SECRET=/X-Admin-Secret: /' > "$HDR"
  curl -sS -X POST "$BASE/api/admin/automation-pause?hours=24" -H @"$HDR"
  rm -f "$HDR"
  ```

- **Fallback if the on-call engineer has no VPS access.** Call the account owner: they hold the SSH key
  and the secret, and the bulk revoke, the pause and the migration steps are theirs. Meanwhile the
  owner can, without the host, revoke each operator token one at a time: **Account → Security → sign
  out** the device, or `POST /api/user/sessions/revoke` with `{"session_id": <id>}` (a stepped-up
  human session is required). That stops each token but cannot pause automation or revoke in bulk, which
  need the host.

1. **Pause, and stop new tokens, before you revoke.** The revoke-to-revert window is the dangerous one:
   until the revert is live, anyone who can mint can mint again. Pause automation if it is not
   already paused (`POST /api/admin/automation-pause` with the secret), and **nobody mints an operator
   token until the revert is deployed**.
2. **Save a pre-count**, and paste the result into the incident note:

   ```sql
   SELECT id, user_id, expires_at FROM sessions WHERE scope = 'operator' AND revoked_at IS NULL;
   ```

3. **Revoke every active operator session**:

   ```sql
   UPDATE sessions SET revoked_at = NOW() WHERE scope = 'operator' AND revoked_at IS NULL;
   ```

   The statement is **safe to re-run**: it only touches rows that are still live, so a second run
   changes zero rows. Or one token at a time: Account → Security, or `POST /api/user/sessions/revoke`
   with `{"session_id": <id>}` (step-up required). **A bulk SQL revoke writes no audit row**; record what
   was run, when, by whom and how many rows it changed.
4. **Revert** the change (see "The migration after the revert" first), deploy it, and wait for the
   release to be live.
5. **Verify, and again after the deploy.** Run the count; it must be `0`:

   ```sql
   SELECT COUNT(*) FROM sessions WHERE scope = 'operator' AND revoked_at IS NULL;
   ```

   Re-run it after the revert deploys, check that no `operator_token_minted` event appeared in the
   window, and confirm a former operator token answers `401` or `403`.

**Scoping an incident.** The audit table is `auth_audit_log` (columns `id`, `user_id`, `email`, `event`,
`ip_hash`, `user_agent`, `session_id`, `success`, `details` (JSON), `created_at`):

```sql
SELECT id, created_at, user_id, session_id, event, success, details
  FROM auth_audit_log
 WHERE event IN ('operator_token_minted', 'operator_access', 'operator_action', 'operator_approval')
   AND created_at >= '<start of window, UTC>'
 ORDER BY id;
```

**The migration after the revert.** `git revert` of the merge also deletes
`V20261104170555__add_users_account_kind.sql`, but the migration was already **applied**: its row stays
in the `flyway_schema_history` table and the `users.account_kind` column stays in `users`. `compose/local/database/flyway-entrypoint.sh`
runs `flyway repair` and then `flyway migrate` on **every deploy**; whether that pair tolerates an applied
migration whose file has gone is not something this repository verifies, so do not rely on it. **Keep the migration file in the revert**: after `git revert`, restore it
(`git checkout <merge sha> -- compose/local/database/migrations/V20261104170555__add_users_account_kind.sql`)
and commit it. The column is harmless once the code is gone: every row reads `standard` and nothing
else reads it. To remove the column, do it with a **new forward migration** (`V<timestamp>__drop_users_account_kind.sql`
containing `ALTER TABLE users DROP COLUMN account_kind;`), never by hand, so the history and the schema
stay in step. After a revert deploys, check what Flyway recorded:

```sql
SELECT version, description, success FROM flyway_schema_history ORDER BY installed_rank DESC LIMIT 5;
```

`success` must be `1` on every row, and the `add_users_account_kind` row should still be present. A
`0`, or a deploy that failed at the migration step, means the history and the files disagree: stop and
restore the migration file as above rather than repairing by hand. Before any `DROP`, save the accounts that were flagged, so the owner knows who loses
operator approval:

```sql
SELECT id, account_kind FROM users WHERE account_kind <> 'standard';
```

## Where the roles differ on approval

| Account kind (`users.account_kind`) | `agent` | `operator` | `observer` |
|---|---|---|---|
| `standard` (default) | queue only | queue only | read only |
| `internal` / `sandbox` | queue only | queue, and **approve explicitly** (audited) | read only |

The passive `auto_approve` default never applies to a machine seat, and no seat may change caps or
approval modes, on any account kind. Operator actions that are not reads, and every operator approval,
are written to `auth_audit_log` with the role.
