# Admin user management (issue #1450)

The design behind the `/admin/users` surface: what an operator can see, what they can change, and —
for every action considered — whether it should exist at all. Written before the code, reviewed
adversarially before the code (§4), and kept here because the interesting parts are the refusals.

**One-line summary of the verdict:** a read-heavy list + detail surface, with exactly ONE write —
grant/revoke admin — gated on step-up, audited, and refused when it would leave the deployment with
no admin. Everything else that a user-management screen usually ships (disable, subscription
override, delete, roles) is argued down to a follow-up or out entirely, below.

## 0. The ground the design stands on

- **`is_user_admin(user_id)` (`utilities/db.py`) is the ONE place admin is decided** (#793):
  `users.is_admin` **OR** a match in the `ADMIN_USER_EMAILS` env allowlist, failing CLOSED. Every
  route here resolves admin through it and re-derives nothing.
- **`_require_user_admin(session_token)` (`api/routers/admin.py`) is the browser admin gate** — 401
  on a dead session, 403 on a non-admin. The `X-Admin-Secret` gates (`_require_admin`,
  `_require_api_and_admin`) are the operator/non-browser path and are NOT interchangeable with it;
  a screen a human clicks runs on the session gate.
- **The SPA already has the shape**: `components/AdminRoute.tsx` → `pages/AdminFeedbackPage.tsx`,
  wired in `App.tsx`, nav link behind `isAdmin` in `Layout.tsx`. This surface copies it exactly
  rather than inventing a second admin chrome.
- **`/api/admin/*` is hidden from the published OpenAPI schema** by `_hide_admin_routes_from_schema()`
  (#1020) — derived from the route table, so these routes inherit it. Hidden is NOT gated; the auth
  is what gates.
- **The deployment has ONE user today** (`id=1`, admin, active). Every scale claim below is
  therefore about the shape of the query, not about measured load — said plainly so nobody reads a
  benchmark into it.

## 1. Research — what a user-management screen actually earns

Surveyed at the pattern level (Stripe Dashboard team members, Vercel team members, Fly.io org
members, PostHog project members). These are consumer-of-the-pattern observations, not a feature
audit of those products' current releases.

What every one of them has, and what LEM therefore needs:

| Pattern | Why it survives | LEM's version |
|---|---|---|
| A flat, searchable list keyed on **email** | Email is how a support request arrives ("user X says…"), so it is the lookup key, not the internal id | `q` matches email substring; id shown but not the search key |
| **Role/permission shown in the list**, changed from a per-row control | The single most common admin question is "who else can do this?" | `is_admin` column with its SOURCE, plus a per-row grant/revoke |
| **Account state** (billing / seat / connection) in the list, not behind a click | The other common question — "why is this account not working?" — must be answerable at a glance | `subscription_status`, `subscription_tier`, `linkedin_connection_status`, `last_login` |
| A **detail view** for the long tail | 49 columns cannot be a table | Row expands to a detail drawer, one extra fetch |
| **The last-admin guard** | Every one of them refuses to let you remove the last owner | §4.1 — and LEM's version has a wrinkle these products don't have |

What they ship that LEM should **not** copy at this size:

- **Invite/seat flows.** LEM has no team model — a user is an account, not a seat in someone else's
  org. There is nothing to invite anyone to.
- **Per-user activity feeds / impersonation.** Impersonation is a credential path into another
  person's LinkedIn account; it is the highest-blast-radius feature on the menu and there is no
  operational need at one user. Out, and not deferred — see §3.4.
- **Bulk selection + bulk actions.** A bulk control over a role grant is a lockout waiting to
  happen and buys nothing under ~100 users.

## 2. Schema → screen

`password`, `access_token`, `refresh_token` and `cookies.value` are **encrypted at rest** (AES-256-GCM,
`docs/secrets-at-rest.md`). They never leave the server **in any shape** — not the value, not a
`has_password` boolean. A present/absent boolean over a credential is still an oracle: it tells a
reader which accounts are worth attacking and how far a prior compromise got, and there is no
question on this screen that it answers. The one connection-health question an admin actually asks
is already answered by `linkedin_connection_status`, which is not a credential.

### `users` (49 columns)

| Classification | Columns |
|---|---|
| **Display (list)** | `id`, `email`, `is_admin` (+ derived source), `subscription_status`, `subscription_tier`, `trial_ends_at`, `linkedin_connection_status`, `last_login` |
| **Display (detail only)** | `public_uid`, `linkedin_email`, `linkedin_display_name`, `email_verified_at`, `trial_started_at`, `subscription_current_period_end`, `timezone`, `city`, `country`, `locale`, `content_language`, `location_source`, `blog_url`, `sitemap_url`, `company_linked_in_url`, `updated_at`, `auto_schedule_posts`, `content_buffer_days`, `content_buffer_max_posts`, `last_login_inactivate_delay`, `avatar_disabled`, `avatar_use_post_image`, `avatar_use_carousel`, `avatar_use_video`, `avatar_use_newsletter`, `avatar_caption_overlay` |
| **Admin-editable** | `is_admin` — and nothing else in phase 1 (§3.4) |
| **NEVER leaves the server** | `password`, `access_token`, `refresh_token`, `access_token_expires_in`, `access_token_created_at`, `refresh_token_expires_in`, `refresh_token_created_at` (credentials + their shape), `proxy_url` (carries proxy user:pass), `reply_inbound_token` (a bearer secret embedded in a reply-to address), `stripe_customer_id`, `stripe_subscription_id` (billing-system identifiers that let a holder act in Stripe; the STATUS is what an admin needs), `latitude`, `longitude` (precise home coordinates — `city`/`country` answers every question this screen has), `linked_sub_id`, `linkedin_session_email_sent_at` |

`latitude`/`longitude` deserve their own sentence: they are the Login Location, i.e. roughly where
the person lives, at 7 decimal places. `city` + `country` carries every operational meaning
("is the proxy geography plausible") without shipping a house.

### `onboarding_state`

`started_at` is the closest thing LEM has to a **signup date** — `users` has no `created_at`, only
`updated_at`, which every write moves. `activated_at` is the activation state. Both are **display**,
joined in the SAME query as the list (LEFT JOIN, no N+1). `linkedin_connected_at`, `voice_set_at`,
`first_post_approved_at`, `caps_enabled_at` are detail-only. Nothing here is admin-editable: these
are timestamps of things that happened, and an editable "activated_at" is a lie about history.

### `engagement_preferences`

51 columns of the user's own voice, targeting and caps. **Display in detail, never editable here.**
An admin editing another person's tone, topic filters or daily caps is a content-integrity problem
dressed as support: it changes what LinkedIn sees as that person's voice, with no consent trail. If
an operator must change a cap in an incident, the honest mechanism is the global pause
(`is_automation_paused`), not a silent per-user edit. Phase 1 shows the four numbers that explain
behaviour — `max_comments_per_day`, `max_dms_per_day`, `posts_per_week`, `comment_length` — and
nothing else.

### `sessions`, `user_email_history`

Out of this surface entirely. `sessions.session_token` is a credential hash; the device list is
already the account owner's own screen (`GET /user/security`). Reading another person's device
list has no operational question behind it that the audit log does not answer better.

## 3. The recommendation

### 3.1 Layout

`/admin/users`, behind `AdminRoute`, second link in the existing admin nav group next to
"Feedback Triage". **Table, not cards** — the questions are comparative ("who is on a trial that
ends this week", "who is disconnected"), and a card grid answers comparative questions badly.
**Detail drawer, not a full page**: an admin reading a detail is mid-scan of a list, and a route
change loses the scan position and the filters.

### 3.2 Default columns

Email · Admin · Subscription (status + tier) · LinkedIn · Last login · Signed up. Six, because a
seventh forces a horizontal scroll at laptop width and the seventh candidate (`timezone`) is not a
question anyone opens this screen to ask. Everything else is in the drawer.

### 3.3 Search and filter

- `q` — case-insensitive substring on `email` **and** `linkedin_email`. Substring, not prefix: the
  support request often carries a domain, not a full address.
- `subscription_status`, `linkedin_connection_status` — exact, validated against the ENUM
  vocabulary before they reach SQL.
- `is_admin` — boolean, and it filters on the **effective** answer (column OR allowlist), because a
  filter that disagreed with the badge in the same row would be a bug report.
- Deliberately **not** shipped: date-range filters on signup/trial. They are the first thing to add
  when the list stops fitting on a screen, and at 1 user they are furniture. `?sort=` is likewise
  out — newest-signup-first is the only ordering with a reason behind it today.

### 3.4 Actions — and whether each should exist

| Action | Verdict | Reasoning |
|---|---|---|
| **Grant / revoke admin** | **Ships.** Step-up gated, audited, last-admin-guarded | This is the feature. It is the one thing that today requires a MySQL shell and cannot be reached any other way |
| **Enable / disable an account** | **Deferred to a follow-up issue** | LEM has no per-user disable. `is_automation_paused` is GLOBAL (Redis), and the per-user gate is `get_active_user_ids()`, which reads subscription + token + last-login. A "Disabled" badge that the scheduler does not read is worse than no button: it reports a state that does not exist. Doing it properly is a column plus a read in every lane's gate — a change to automation, not to a screen, and it belongs in its own issue with its own tests |
| **Subscription / trial override** | **Deferred to the same follow-up** | `subscription_status`, `subscription_tier` and `subscription_current_period_end` are written by the Stripe webhook path. An admin override is silently reverted by the next webhook, so the button appears to work and then doesn't — the worst failure shape available. Doing it properly means deciding precedence between an operator override and the billing system, which is a product decision, not a UI one |
| **Account deletion** | **Rejected for this surface** | It is irreversible, it cascades (`ON DELETE CASCADE` reaches onboarding, posts, preferences), and GDPR-style erasure has requirements — proof of request, a record that it happened — that a button in a table does not meet. The right home is a deliberate, logged, operator-run path, not a row action one click from a role toggle |
| **Impersonate / "log in as"** | **Rejected outright** | It is a credential path into someone's LinkedIn automation. There is no operational need at this size and the blast radius is the whole product |
| **Resend verification / password reset** | **Not needed** | LEM's login is an email PIN; there is no password to reset and verification is stamped by the login itself |

### 3.5 Audit trail

Admin role changes write to **`auth_audit_log`** — the table that already records every
security-relevant thing that happens to an account (login, factor added, email changed, session
revoked). Two new `AuthAuditEvent` values, `admin_granted` and `admin_revoked`. **No migration:**
`auth_audit_log.event` is `VARCHAR(50)`, not an ENUM.

`user_id` on the row is the **TARGET** account, and the acting admin rides in `details`
(`{"actor_user_id": N}`). That is the reading `user_id` already has on every other event in that
table — the account this happened TO — and it means the affected user sees "An admin granted you
admin access" in their own Security card, which is exactly who should be told. The reverse
convention (row keyed on the actor) would hide a role change from the person it happened to.

`details` carries ids only. No email, no IP, no session token: the actor's identity is an id the
operator can resolve, and the audit log is not where other people's addresses accumulate.

**Not shipped:** an admin-facing audit VIEWER. The rows exist and are queryable from the moment
this lands, which is the part that cannot be backfilled; a screen over them can be built any time
and answers no question that is being asked at one user. Named in the follow-up issue.

### 3.6 Pagination and scale

Two queries per page load — one `COUNT(*)`, one `SELECT … LIMIT/OFFSET` with a single LEFT JOIN to
`onboarding_state` — and nothing per row. The detail drawer is one further query, on click.
`limit` defaults to 25 and is capped at 100 by `Query(..., le=100)`, so no caller can ask for the
whole table.

Where it stops working: **OFFSET pagination degrades once the offset itself is large** (MySQL walks
and discards the skipped rows), and the `q` substring match is `LIKE '%…%'`, which cannot use the
`email` index. Both are irrelevant below roughly **10,000 users** and neither is worth pre-solving
at 1 — the fix when it comes is keyset pagination on `(id)` plus a prefix or fulltext match, and it
changes the repository function, not the screen. Recorded here so the next reader does not have to
re-derive the ceiling.

### 3.7 Is binary `is_admin` enough?

**Yes — recommendation: keep binary, do not introduce roles.** Not "not yet": there is no second
capability to separate. Everything behind `is_user_admin` today is one job (see the feedback triage
panel, the YouTube token status, and this surface), one deployment, one operator. A role table
introduced before there are two distinct duties encodes a guess about which duties they will be,
and every gate then has to ask a question that has one answer. The trigger to revisit is concrete
and worth writing down: **the first time someone should be able to do A and not B** — a support
contractor who may read the user list but not grant admin is the likely first case. At that point
the change is a `role` column read by `is_user_admin` (still the ONE place), not a rewrite.

## 4. Adversarial review — verdict and what changed

Run as a separate pass against §1–3 before any code was written. Four findings changed the design;
they are listed with what they changed. The rest of the design survived and the "why" is given.

### 4.1 Security

**Finding A (changed the design) — the last-admin guard cannot count `is_admin` alone.**
Admin is `users.is_admin` **OR** the `ADMIN_USER_EMAILS` allowlist. A naive guard
(`SELECT COUNT(*) … WHERE is_admin = 1`) would refuse a perfectly safe revoke on a deployment whose
real admin is an allowlist entry, and — worse in the other direction — a guard that counted only
the allowlist would allow the last column-admin to be removed. **Changed:** the count is the
EFFECTIVE one, `is_admin = 1 OR LOWER(email) IN (allowlist)`, in one query, which is the same
predicate `is_user_admin` decides with.

**Finding B (changed the design) — the guard must fail CLOSED on an unreadable count.**
The first draft returned `0` on a DB error, which is "no admins left" — and would have REFUSED
every revoke, which sounds safe but is the wrong failure for the same code path used by the grant
that fixes a lockout. **Changed:** the counter returns `None` on error and the route answers **503**
(a DB fault is 503 per CLAUDE.md), distinguishing "we know there is one admin left" from "we could
not tell", and never guessing.

**Finding C (changed the design) — self-demotion.**
The question in the issue is "can the last admin remove their own admin bit and lock everyone
out?" The answer must be no, and the last-admin guard alone is *nearly* enough. But a second admin
demoting themselves by accident mid-incident is a real, ordinary mistake, and there is no case
where it is the thing they meant to do — the deliberate version is "ask the other admin to remove
me." **Changed:** self-revoke is refused unconditionally (409), independently of the count, with a
message that says who can do it instead. Self-GRANT is not a thing (you already are one).

**Finding D (checked, no change) — `_AGENT_SESSION_SURFACE`.**
The `agent` scope may queue but never approve, and **surfaces match on PATH not method**, so
granting a read grants its writes. `/admin/users`, `/admin/users/{id}` and
`/admin/users/{id}/admin` are therefore **absent** from every scope surface — which is the default
(`_scope_allows` fails closed on an unknown path), but "absent by default" is exactly the kind of
thing that is true until someone adds a line. Pinned by a test that asserts the absence, so adding
that line fails the build.

**Finding E (checked, no change) — CSRF.** The role change is a cookie-authenticated POST, so
`X-LEM-Client` is required. It is enforced in the ONE resolver (`get_session_user_id`, cookie
branch) and the SPA's axios client sends it on every request — no call-site work, and a new route
cannot forget it.

**Finding F (changed the design) — step-up.**
`sessions.last_verified_at` gates every credential-touching write. Granting admin is not touching a
credential — it is minting the authority to touch everyone's. **Changed:** the role change is
step-up gated, reusing `_require_step_up` (the ONE refusal shape, `403 step_up_required` + the
available methods) rather than a second one. Note the honest limitation: `step_up_satisfied` returns
True when the account holds no strong factor, so on a deployment with no passkey/TOTP enrolled this
gate is a no-op. That is the documented contract of the whole step-up layer, not a hole opened here.

**Finding O (changed the design) — revoking an ALLOWLIST admin writes nothing.**
`ADMIN_USER_EMAILS` is env, not data. Clearing `is_admin` on an account that is an admin *because
of the allowlist* changes a column that was already 0 and leaves the person an admin — a button
that reports success and does nothing, which is how an operator ends up believing access was
removed. **Changed:** the route reads the target first and refuses that revoke with **409** and a
message naming `ADMIN_USER_EMAILS` as the thing to edit. The same read makes a redundant
grant/revoke an explicit `changed: false` no-op instead of a failed `UPDATE` (MySQL reports zero
changed rows when the value already matches, which is indistinguishable from "no such user").

**Finding G (checked, no change) — privilege escalation via the target parameter.** `user_id` in
the path is a TARGET, never the actor; the actor is `_require_user_admin`'s return value. A
non-admin naming an admin's id gets 403 before anything reads the target.

### 4.2 Usability

**Finding H (changed the design) — "why is this account doing nothing?" was unanswerable.**
The first column set was email / admin / subscription / signup. An operator's actual day-one
question is why an account is idle, and the answer is nearly always the LinkedIn connection or a
lapsed login — neither of which was on the screen. **Changed:** `linkedin_connection_status` and
`last_login` are default columns; `timezone` (the previous candidate) moved to the drawer.

**Finding I (checked, no change) — noise.** The 26 detail-only columns are behind a click, and the
avatar toggles are the weakest of them. Kept: they are the reason an account's images look wrong,
which is a real support question, and a drawer is where a long tail belongs.

### 4.3 Privacy

**Finding J (changed the design) — `proxy_url` and `reply_inbound_token` were not on anyone's
never-list.** Neither is encrypted at rest, so neither is caught by the secrets-at-rest reflex, and
both are credentials: the proxy URL embeds `user:pass`, and the reply token is a bearer secret in an
email address. **Changed:** both are named explicitly in the never-leaves-the-server row, and the
response is built from an explicit field list, never `SELECT *` handed to the serializer.

**Finding K (changed the design) — lat/long.** Precise coordinates were in the detail draft under
"geo". **Changed:** dropped; `city`/`country`/`location_source` stay.

**Finding L (checked, no change) — no credential present/absent booleans.** Explicitly argued in
§2 rather than left implicit, because "just a boolean" is how this leak normally arrives.

### 4.4 Scale

**Finding M (checked, no change) — N+1.** The list is one JOIN, not a per-row lookup, and the
effective-admin badge is computed from the row's own email against an in-process allowlist, not a
query per row. The count is a second query, not a `SQL_CALC_FOUND_ROWS`.

**Finding N (changed the design) — an uncapped `limit`.** The first draft took `limit: int = 25`
with no bound. **Changed:** `Query(25, ge=1, le=100)`, matching the feedback panel's shape.

## 5. What ships in this PR

- `GET /api/admin/users` — filtered, paginated list (+ `total`).
- `GET /api/admin/users/{user_id}` — detail.
- `POST /api/admin/users/{user_id}/admin` — grant/revoke, step-up gated, audited, guarded.
- `pages/AdminUsersPage.tsx` behind `AdminRoute`, nav link behind `isAdmin`.
- `admin_granted` / `admin_revoked` in `AuthAuditEvent` + their labels in the user's own Security card.
- No migration. No schema change.

Deferred, with the reasoning above: per-user disable, subscription/trial override, an admin-facing
audit viewer, date-range filters. Filed as a follow-up issue and linked from the PR.
