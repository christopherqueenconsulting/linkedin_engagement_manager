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

## CSRF

Cookie auth means a state-changing request can now be authenticated by something the browser
attaches automatically, which is the shape CSRF exploits. Three things stand between that and a
forged write:

- **`SameSite=Lax`** — the cookie is not attached to a cross-site POST at all. It rides only on
  top-level GET navigations, which is exactly what the LinkedIn OAuth return trip needs and nothing
  more. (`strict` would break that return trip; that is why it is `lax` and not tighter.)
- Every mutating endpoint is `POST`/`PUT` with a JSON body — a form POST from another origin cannot
  set `Content-Type: application/json` without a preflight, and no CORS middleware is installed, so
  the preflight has nothing to succeed against.
- In deployments with `API_ACCESS_TOKENS` set, `/api/*` also needs the bearer token, which lives in
  the SPA bundle and not in the browser's ambient credentials.

If a future change adds CORS with credentials, or a form-encoded mutating endpoint, this section is
the thing that has to be revisited first.

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

## Still open after 2b

Phase **2c** — passkeys (`webauthn`) + TOTP (`pyotp`) + recovery codes (`argon2-cffi`) + a step-up
gate on credential-touching endpoints, with the email PIN demoted to bootstrap-only. Its prerequisite
is a valid TLS certificate on the public hostname (WebAuthn requires a secure context).
