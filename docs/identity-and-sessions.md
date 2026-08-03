# Identity & sessions — hardening (issue #745, PR 2b)

The design, threat model and the rest of the Phase-2 plan live in
[`AUTH_SECURITY_DESIGN.md`](AUTH_SECURITY_DESIGN.md); [`secrets-at-rest.md`](secrets-at-rest.md) is
2a (encryption of the LinkedIn secrets). This file is the **operator + reviewer** half of 2b: what
changed about who a user is, how a session is proven, and what stops a PIN being guessed.

## What changed

| Before | After (2b) |
|---|---|
| `sessions.session_token` stored the token in plaintext | stores `SHA-256(token)` — a DB dump yields no live session |
| the token lived in `localStorage`, readable by any script on the page | lives in an **httpOnly** cookie the browser attaches itself |
| one session row per login, no way to see or end them | per-device rows (label, pseudonymised IP, last seen) with revocation |
| the email address WAS the identity | `users.public_uid` is the identity; email is an attribute that can move |
| `/auth/email/init` + `/verify` unlimited against a 6-digit PIN | per-email + per-IP limiter, plus a durable PIN lockout |
| no record of logins, failures, or session changes | every one of them appended to `auth_audit_log` |

## Session tokens

`create_session()` mints `secrets.token_hex(32)` and returns it to the caller **only**; the row gets
`crypto.hash_session_token(token)`. Lookup (`get_session_user_id`) hashes the presented token before
it touches SQL, so the plaintext never reaches the database in any query.

The hash is deliberately **unkeyed** SHA-256, not an HMAC under `LEM_SECRET_KEY`: the token is 256
bits of randomness, so there is nothing to brute-force, and keying it would mean a lost or rotated
master key logs every user out. The IP hash IS keyed when a master key exists — an IP is a ~2^32
space that a plain digest does not protect.

The migration rewrites existing rows in place (`UPDATE sessions SET session_token =
LOWER(SHA2(session_token, 256))`), so **nobody is signed out by the deploy**: the browser still holds
the same plaintext token and the server hashes it on the way in. The UPDATE is unconditional on
purpose — a token is 64 hex characters and so is its hash, so length cannot tell them apart, and
Flyway runs a migration exactly once.

## The cookie, and why the SPA sends `"cookie"`

Login sets `lem_session` (`SESSION_COOKIE_NAME`) — `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`,
`Max-Age` = the **absolute** session cap (`SESSION_ABSOLUTE_MAX_DAYS`). The idle window still slides
server-side; a cookie that expired mid-idle-window would sign an active user out.

Roughly 150 SPA call sites pass `session_token` in a body or query string. Rather than rewrite all of
them, `AuthContext` now holds the non-secret sentinel `COOKIE_SESSION = 'cookie'`, and
`api/main.get_session_user_id()` resolves each request in this order:

1. an explicit token that is not the sentinel **and resolves** — non-browser callers, the LinkedIn
   OAuth `state` round trip, the tutorial capture harness;
2. otherwise the request's `lem_session` cookie (read off a `ContextVar` set by
   `session_cookie_middleware`, so handlers that never took a `Request` still get cookie auth).

A stale explicit token falls **through** to the cookie rather than 401ing: a browser holding a token
from before the cutover is still the signed-in person on that cookie. `current_session_token()`
follows the SAME order — an explicit token only wins there if it resolves — so the two can never
name different sessions for one request. They must not: logout would delete a row that is already
gone and leave the live session signed in, and "sign out all other devices" would fail to match the
caller's own session as the one to keep and revoke it.

`login()` in `AuthContext` verifies the cookie stuck by reading the session back; if that fails (an
`http://` origin with `Secure` cookies, or a browser blocking them) it falls back to holding the
real token, so a valid login is never turned into a lockout. Set `SESSION_COOKIE_SECURE=false` for a
plain-http local origin.

## The parameter is a target, never the actor (issue #914)

`get_session_user_id` only means something on the routes that call it, and until #914 a set of them
did not. `PUT /user/`, `GET|DELETE /posts/`, `POST /posts/bulk_update/`, `POST /update_post/`,
`GET /post_url/`, `GET /dashboard/stats/`, `GET /dashboard/planned-tasks/`, `GET /activity/`,
`GET /user_id/`, `POST /schedule_post/`, `POST /create_weekly_content/`,
`POST /invite_to_li_company_page/`, `POST /automate_reply_commenting` and
`POST /aws_test_get_my_profile/` read the acting account out of an `email` / `user_id` / `post_id`
**request parameter**. The only thing in front of them was the shared bearer token
(`API_ACCESS_TOKENS`), which the SPA shipped in its build (`VITE_API_TOKEN`) — so it was held by
everyone who had ever loaded the page. (**#950 retired that**; see the next section.) `PUT /user/`
was the worst of them: it MOVED the account email given only the current one, which is the whole
account for one query parameter.

Every one of them now resolves the caller through **`require_session_user_id()`** — the 401 wrapper
around the same resolver — and treats what the request named as a target to authorise:

| Helper | Rule |
|---|---|
| `require_session_user_id(token)` | the acting user, or **401**. Nothing below runs without it. |
| `_reject_foreign_email(user_id, email)` | an `email` parameter must be the caller's own → **403** |
| `_reject_foreign_user_id(user_id, target)` | same, by id |
| `_require_own_posts(user_id, post_ids)` | **403** unless `db.user_owns_posts` proves EVERY id |

Three deliberate choices in there:

- **The parameter is checked, not ignored.** Answering a mismatch with the caller's own data would
  be a silent substitution, and a legacy client naming its own address keeps working either way.
- **`user_owns_posts` fails closed.** An empty list and a missing row both answer False — "we could
  not prove ownership" must never be spelled the same way as "they own it". A batch is rejected
  whole: a list is only as scoped as its worst entry.
- **403, not 404-per-id.** Which post ids exist is the enumeration these endpoints used to hand out.
- **An outage is not a permission error.** A database fault raises `db.OwnershipUnprovable` and
  `_require_own_posts` answers **503**. The action is refused either way — that is the fail-closed
  half and it does not move — but "you may not touch these posts" and "we could not find out" are
  different facts. Collapsing them tells a user they lack permission to their own drafts, sends
  on-call hunting an authorisation bug, and files a security-shaped defect through `_deny`'s
  recurrence escalation every time the database blips.

A denied TARGET is logged (`log_warning`, so the recurrence escalation files it) AND audited
(`auth_audit_log`, `AuthAuditEvent.FOREIGN_TARGET_DENIED`) because a caller who resolved a session
and then named another account is a broken client or somebody working this hole. The log line is
greppable; the audit row is queryable per account, which is the shape of the question you actually
ask ("has THIS user been naming other people's accounts?"). Only the KIND of identifier and the path
go into it — never the caller-supplied value, which is somebody else's address. A **401 is neither**,
and deliberately: sessions expire in the ordinary course of things and the SPA polls, so warning on
one would file a defect for working behaviour (`utilities/CLAUDE.md`).

The post-mutating writes carry the same rule twice. `bulk_update_posts`, `soft_delete_posts`,
`update_db_post` and `update_db_post_rejection_reason` take an optional `user_id=` that scopes their
`WHERE` clause, and the API passes it. The check in front is the gate; the scope is what closes the
window between the check and the write, and what makes a future caller that forgets the check
harmless rather than cross-account — which is only true if EVERY write on the table carries it.

`get_post_by_email` is gone rather than deprecated. It turned an ADDRESS into somebody's posts,
which is the exact shape `GET /posts/` used to authenticate on; its one caller resolves the session
now and calls `get_posts(user_id, …)`, so leaving the wrapper behind would keep an address-keyed
reader one import away from the next endpoint.

**The cache in the browser is the other half.** Since 2b the SPA's `sessionToken` is the same
non-secret `'cookie'` sentinel for every account, so a React Query key carrying it carries no
identity at all — sign out, sign in as somebody else in the same tab, and the previous account's
dashboard renders out of the cache. User-scoped keys carry the user id, and `logout()` calls
`queryClient.clear()`, which is the structural half.

`new_email` no longer moves the account from `PUT /user/`: the address moves through
`POST /user/email/change/init|verify`, which PINs the NEW address, is step-up gated, and revokes
every other session. The field stays **declared** on the request model and answers **400** with a
pointer — dropping it would let Pydantic discard it and answer 200, and a silent success on an
email change is how somebody believes their address moved when it did not.

`POST /generate-carousel` was importing `db.get_session_user_id` directly, so it never saw the
cookie sentinel (and no session scope reached it). It goes through the module resolver like
everything else now — **there is one resolver, and a route that imports around it is a bug.**
`tests/unit/api/test_param_auth_scoping.py` is the standing proof: one 401 case and one 403 case per
converted route, each asserting the db call behind it was never reached.

## The `/api` bearer token is a non-browser credential (issue #950)

#914 made the session the identity everywhere, which left `API_ACCESS_TOKENS` **still checked and
no longer sufficient for anything**. It was also still being handed to every visitor: the SPA read
it from `VITE_API_TOKEN`, and Vite inlines a `VITE_*` at build time, so it was a constant in a
public bundle. A secret everyone holds is not a secret — and carrying it as one meant it read as a
layer in threat models it was not one in, and could not be rotated without rebuilding and
redeploying the SPA.

So the SPA ships none, and the token's contract is now written down rather than assumed:

- **The browser never holds it.** `ui/src/api/client.ts` sends no `Authorization` header, the
  Dockerfile has no `VITE_API_TOKEN` ARG, and `.github/workflows/ui-build.yml` builds with CANARY
  values for the LEM-issued `VITE_*` names and greps `dist/` for them. Read one back into the
  bundle and UI Build fails. (`VITE_POSTHOG_KEY` is deliberately not canaried — a write-only
  third-party ingest key is *meant* to be public.)
- **The middleware asks only "did this caller bring A credential"** — a valid bearer, or a session
  credential (the `lem_session` cookie, or the `X-Session-Token` header the SPA sends on the
  cookie-less fallback). Presence, not validity: the middleware runs before routing and has no
  database, and the route's own `require_session_user_id()` already fails closed. It is an edge
  filter that keeps credential-less traffic off the handlers. **It is not authorisation** — a
  forged cookie clears it and is then refused by the route, which is the same 401 from one step
  further in.
- **`API_ACCESS_TOKENS` stays set in production**, rotatable in the server `.env` alone now that no
  build artifact has to match it. `_require_api_and_admin` (the `/api/admin/*` routes) still demands
  it *and* `X-Admin-Secret`.

Every non-browser caller of `/api`, and what it authenticates on:

| Caller | Credential |
|---|---|
| SPA (`ui/src/api/client.ts`) | session cookie — **no bearer** |
| Tutorial capture harness (`marketing/video_tutorials.py`) | drives the SPA with a real session token in `localStorage` → `X-Session-Token` |
| Browser extension (`browser_extension/popup.js`) | none — posts only to the public, self-authenticating `/api/user/linkedin-cookie` with the user's `session_token` in the body |
| `scripts/generate_media_variants.sh` | bearer **+** `X-Admin-Secret` (an `/api/admin/*` route) |
| Postman collection (`docs/postman/`) | bearer, from the server `.env` |
| Stripe webhook | none — signature-verified, in `_PUBLIC_API_PREFIXES` |
| LinkedIn OAuth return trip | not under `/api` (`/auth/linkedin/*`), never gated here |
| Celery tasks | in-process; they call `db.py` directly and never loop through HTTP |

`db.update_user` lost its `email=` parameter in the same change. #914 removed its last caller, but
it still moved an account's address with a bare `UPDATE` — no `user_email_history` row, no PIN to
the new address, no session revoke — which is every guarantee `POST /user/email/change/init|verify`
exists to make. `email = %s` is out of `_ALLOWED_USER_CLAUSES` too, so the clause cannot be
reintroduced by a caller alone.

## CSRF

Cookie auth means a state-changing request can now be authenticated by something the browser
attaches automatically, which is the shape CSRF exploits. **Two** things stand between that and a
forged write:

- **`SameSite=Lax`** — the cookie is not attached to a cross-site POST at all. It rides only on
  top-level GET navigations, which is exactly what the LinkedIn OAuth return trip needs and nothing
  more. (`strict` would break that return trip; that is why it is `lax` and not tighter.)
- Almost every mutating endpoint is `POST`/`PUT` with a JSON body — a form POST from another origin
  cannot set `Content-Type: application/json` without a preflight, and no CORS middleware is
  installed, so the preflight has nothing to succeed against.
**There used to be a third, and #950 removed it — say so plainly.** In deployments with
`API_ACCESS_TOKENS` set, `/api/*` also needed the bearer token, and a cross-site form POST cannot
set an `Authorization` header even when the attacker knows the value, so it *did* work here despite
being public. It was worthless as access control and real as a CSRF layer; retiring it from the
bundle trades the second for the first. What replaced it is not a CSRF layer: the middleware accepts
a session credential, and the browser attaches the cookie by itself.

**"Almost" is exact, and #914 is why.** Four mutating routes take query parameters and no body —
`POST /create_weekly_content/`, `POST /invite_to_li_company_page/`, `POST /aws_test_get_my_profile/`
and `POST /automate_reply_commenting`. Before #914 they authenticated on a `user_id`/`post_id`
parameter, so the cookie was irrelevant to them; now they resolve the session like everything else,
which is what puts them under this heading at all. A cross-site form POST reaches them without a
preflight, so for those four the JSON-body layer is not there and **`SameSite=Lax` is the whole
defence** — one layer, not two, since #950. It holds on its own (the cookie is simply not attached
to a cross-site POST), and each of the four only ever queues work for the CALLER's own account, so
the worst a forged one buys is a job the user could have started themselves. But one layer is one
layer, and a new query-parameter mutating route inherits it. A non-secret custom request header the
SPA sends and a forged form cannot — the standard replacement — is tracked on **#957**.

If a future change adds CORS with credentials, or another form-encoded/query-only mutating endpoint,
this section is the thing that has to be revisited first.

## Per-device sessions

`GET /api/user/security` returns the account's live sessions (row id, label, first/last seen) plus
the recent `auth_audit_log` entries. It never returns a token, a token hash or an IP hash.

`POST /api/user/sessions/revoke` takes `{session_id}` or `{all_others: true}`. Single revocation is
scoped by `user_id` in SQL — an id belonging to another account is a 404, never a cross-account
revoke. Revocation sets `revoked_at`, and `get_session_user_id` refuses revoked rows.

The SPA surface is `SecurityCard.tsx` (Account & Billing section).

## Email as an attribute

`users.public_uid` (UUID, unique, backfilled for every existing row) is the identifier that may
appear in a URL, a log line or a support ticket. The sequential row id stays server-side.

`users.email_verified_at` is stamped only where a PIN was actually consumed — the email-PIN login
and the email change. The **no-mail-provider bypass login deliberately does not stamp it**: nobody
proved control of the address on that path, and 2c's step-up gate is meant to be able to trust the
column.

Changing an address is two calls: `/api/user/email/change/init` sends a PIN **to the new address**,
`/api/user/email/change/verify` consumes it and moves the account. On success every OTHER session is
revoked — an email change is exactly what an attacker does after stealing a session, so taking the
address back has to end those sessions. The move is written to `user_email_history` with the session
that made it.

Two deliberate refusals: an address owned by another account gets the same generic 400 as any other
rejected address (a distinct "already registered" reply would be an account-existence oracle), and
with no mail provider configured the flow is a 503 rather than an unconfirmed change — unlike login,
this flow has no bypass, because proving control of the new address IS the flow.

## Rate limiting

Two layers, deliberately different in kind:

- `utilities/auth_rate_limit.py` — fixed-window counters in **Redis**, per email and per client IP
  (`AUTH_INIT_MAX_PER_HOUR` 5, `AUTH_VERIFY_MAX_PER_HOUR` 10, `AUTH_IP_MAX_PER_HOUR` 30). It **fails
  open**: the API must not stop authenticating people because the broker restarted. A successful
  login clears the counters, so four fat-fingered PINs don't cost the rest of the hour.
- `db.verify_pin_for_email` — a **durable** attempt counter on `email_pin_auth`. After
  `PIN_MAX_ATTEMPTS` (5) wrong guesses the outstanding PIN is locked for `PIN_LOCKOUT_MINUTES` (15),
  and even the correct PIN is refused while the lock stands.

A new `/auth/email/init` clears the unused PIN rows and therefore the lock — that is what the
per-email init limiter bounds. Worst case is ~25 guesses an hour against a 10^6 space.

### Which address the per-IP bucket counts

`_client_ip` reads **`CF-Connecting-IP` first**, and that ordering is load-bearing. Cloudflare sets
that header on everything it proxies and overwrites whatever the client sent, so it is the one value
an attacker cannot choose. `X-Forwarded-For` is only the fallback for a deployment with no
Cloudflare in front: a proxy *appends* to the chain the client supplied, so its first entry is
attacker-controlled — reading that as the client would let one host mint itself a fresh per-IP
bucket per request and write a forged `ip_hash` into `auth_audit_log`. If a different edge is ever
put in front of the app, whatever header IT overwrites is what belongs at the top of that function.

## `auth_audit_log`

Every login (success, failure, rate-limited, PIN-locked), logout, session revoke and email change is
appended by `db.record_auth_event`. `user_id` is nullable on purpose: a failed login against an
unknown address has no account. The write is best effort — an audit failure must never fail a login
— and the raw IP is never stored, only its keyed hash.

## Environment

| Variable | Default | Why you'd change it |
|---|---|---|
| `SESSION_COOKIE_NAME` | `lem_session` | rarely |
| `SESSION_COOKIE_SECURE` | `true` | `false` for a plain-http local origin, or the browser drops the cookie |
| `SESSION_COOKIE_SAMESITE` | `lax` | `strict` breaks the LinkedIn OAuth return trip |
| `AUTH_INIT_MAX_PER_HOUR` | `5` | PIN emails per address per hour |
| `AUTH_VERIFY_MAX_PER_HOUR` | `10` | PIN submissions per address per hour |
| `AUTH_IP_MAX_PER_HOUR` | `30` | auth calls per client IP per hour |
| `PIN_MAX_ATTEMPTS` | `5` | wrong guesses before the PIN locks |
| `PIN_LOCKOUT_MINUTES` | `15` | how long the lock stands |

## What 2c changed about this

Phase **2c** shipped: passkeys, TOTP, recovery codes and a step-up gate — see
[`strong-authentication.md`](strong-authentication.md). Three things on this page moved:

- The **email PIN is a bootstrap**, not a login, for an account that enrolled a strong factor.
  `email_verified_at` still means the same thing (a PIN was consumed at that address) — it is
  stamped before the second-factor branch.
- **`/api/user/sessions/revoke` and `/api/user/email/change/init` are step-up gated.** Both are what
  an attacker holding a stolen session does first. An account with no strong factor still passes,
  so nothing changed for a user who has not enrolled.
- **`/api/user/extension-token` now mints an `extension`-scoped session** and is itself step-up
  gated. That scope is what later lets the extension POST a `li_at` without a WebAuthn ceremony it
  could never run.
