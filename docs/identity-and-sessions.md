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

That `ContextVar` is **module state**, which makes the module's IDENTITY part of the auth path.
`api/main.py` may only ever be imported as `cqc_lem.api.main`. Both `/app` and `/app/src` are
importable in the container, so `src.cqc_lem.api.main` loads the same file a SECOND time as a
distinct module object with its own ContextVars — and serving the app from that copy (`uvicorn
src.cqc_lem.api.main:app`, which is what the start script said until #1354) means
`session_cookie_middleware` publishes the cookie where no `api/routers/*.py` handler can read it.
Cookie auth then returns `None` for every router-served route while the handful of routes still
defined in `main.py` keep working, so **sign-in succeeds and everything after it 401s** — which the
SPA's 401 interceptor turned into "cannot get past the login screen" (the amplification #1358 then
removed: see *A 401 is about the endpoint* below). Nothing raises. The guards are
`_guard_canonical_module()` (CRITICAL on an aliased import) and
`tests/unit/api/test_canonical_module_identity.py`, which pins the uvicorn target in
`compose/local/fastapi/start*`.

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
  Dockerfile has no `VITE_API_TOKEN` ARG, and `.github/workflows/ui-build.yml` enforces both ends:
  it greps `src/` for a `VITE_API_TOKEN` / `VITE_ADMIN_SECRET` read (the diff that reintroduces
  one), then builds with CANARY values for those names and greps `dist/` (the bundle that already
  did). The bundle grep carries a **positive control** — a fixed fake `VITE_POSTHOG_KEY` that must
  be present in `dist/` — because a negative grep only proves something if it could have found
  anything; without it, a change to Vite's inlining would turn the canary into a silent pass.
  (`VITE_POSTHOG_KEY` is deliberately not canaried — a write-only third-party ingest key is *meant*
  to be public, which is exactly what qualifies it as the control.)
- **The middleware asks only "did this caller bring A credential"** — a valid bearer, or a session
  credential (the `lem_session` cookie, or the `X-Session-Token` header the SPA sends on the
  cookie-less fallback). Presence, not validity: the middleware runs before routing and has no
  database, and the route's own `require_session_user_id()` already fails closed. It is an edge
  filter that keeps credential-less traffic off the handlers — and only the naive kind, since one
  arbitrary cookie byte clears it. **It is not authorisation** — a forged cookie clears it and is
  then refused by the route, which is the same 401 from one step further in.
- **That safety argument is a TEST, not a paragraph.** Loosening a global edge control is only safe
  while every gated route really does resolve its caller, so
  `tests/unit/api/test_api_route_identity.py` walks the live route table and asserts it: every
  `/api` path outside `_PUBLIC_API_PREFIXES` reaches either the session resolver or the admin
  secret. The two sets are derived by closing over the call graph from `get_session_user_id` /
  `_require_admin`, so a new wrapper counts automatically and a route that leans on the bearer alone
  fails the build. `_NO_IDENTITY_BY_DESIGN` is the one escape hatch and each entry has to argue the
  route reads nothing user-scoped.
- **`API_ACCESS_TOKENS` stays set in production**, rotatable in the server `.env` alone now that no
  build artifact has to match it.
- **`/api/admin/*` is NOT uniformly a two-credential surface, and #950 is why — say so plainly.**
  Eighteen admin routes run on three different gates, and the middleware used to add the bearer on
  top of all of them. All three live in `api/routers/admin.py` alongside the routes since #1154 —
  unlike every other router slice, which reaches the auth kernel as `_main.<name>` at request time,
  these could not: six handlers take `_require_api_and_admin` as a `Depends()` default argument,
  which binds at import time. Nothing outside `/api/admin/*` reads any of them.

  | Gate | Routes | Credentials after #950 |
  |---|---|---|
  | `_require_api_and_admin` | the five `/admin/test/*` runs + `/admin/consolidate-duplicate-comments` + `/admin/task-status/{id}` (6) | bearer **and** `X-Admin-Secret` — it re-checks the bearer itself, so this pair is unchanged |
  | `_require_admin` | `/admin/automation-{pause,resume,status}`, `/admin/fix-video-urls`, `/admin/user/location`, `/admin/regenerate-{carousel,video}`, `/admin/generate-media-variants`, `/admin/youtube-token` (9) | `X-Admin-Secret` **alone** — the bearer was the middleware's, and the middleware no longer demands it |
  | `_require_user_admin` | `/admin/feedback`, `/admin/feedback/{id}/review`, `/admin/youtube-status` (3) | an **admin session** — these are SPA pages, so they never could hold a bearer |

  For the twelve non-`_require_api_and_admin` routes this is a layer removed. It costs nothing
  *today* — every bearer value is public, so a caller who has `ADMIN_SECRET` has one too — but it
  stops being free the moment **#965** rotates them, and that is exactly the kind of book-keeping
  this section exists to keep honest. It stopped being free on 2026-08-07, when #965 rotated them.
  `X-Admin-Secret` is a custom header, so none of the three
  gates is reachable by a cross-site form. `tests/unit/api/test_api_route_identity.py` pins the
  invariant that survives: every `/api/admin/*` route reaches one of the three.

**Rollout, and the part that is not free.** The change is breakage-free in both directions — an old
SPA bundle cached across the release still sends the bearer (still valid), a new one sends the
cookie — but *"still valid"* is doing real work in that sentence. **Every token value that was ever
shipped in a bundle is a known-public credential**, and this PR retires the shipping mechanism, not
the leaked secret: post-merge those values still clear the edge filter, and still pair with
`X-Admin-Secret` on `/api/admin/*`. They keep working only so the cached-bundle window (a browser
tab open across the release — hours, not days; see `docs/spa-deploy-freshness.md`) does not 401. So
the rotation is the second half of the fix, not a nice-to-have: rotate `API_ACCESS_TOKENS` in
`/opt/lem/.env` once that window has passed, and delete the now-unread `UI_API_TOKEN` repo secret.

**Done 2026-08-07 (#965, closed).** `API_ACCESS_TOKENS` was rotated in `/opt/lem/.env`, every app
service recreated (standby and workers first, the active color last), and the `UI_API_TOKEN` repo
secret deleted. Verified at the edge: an old value now answers **401**, a new one clears the filter.
So every token value that shipped in a bundle before that date is dead, and the layer described
above is load-bearing rather than book-keeping. Nothing else needed changing — the one script that
uses a token reads it out of the env file at runtime, and the Postman environment stores
placeholders, not real values.

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
attaches automatically, which is the shape CSRF exploits. **Three** things stand between that and a
forged write:

- **`SameSite=Lax`** — the cookie is not attached to a cross-site POST at all. It rides only on
  top-level GET navigations, which is exactly what the LinkedIn OAuth return trip needs and nothing
  more. (`strict` would break that return trip; that is why it is `lax` and not tighter.) Since
  #950 the cookie is the browser's ONLY credential, so its attributes are pinned by test rather
  than by review: `TestSessionCookieAttributes` asserts `httponly` / `secure` / `samesite=lax` /
  `path=/` on what `_set_session_cookie` issues, that an unset or unusable `SESSION_COOKIE_SAMESITE`
  falls back to `lax` instead of 500ing a login, and that the four query-parameter routes below are
  `POST`-only — `Lax` *does* send the cookie on a top-level GET, so a state-changing `GET` would be
  forgeable by a bare link.
- Almost every mutating endpoint is `POST`/`PUT` with a JSON body — a form POST from another origin
  cannot set `Content-Type: application/json` without a preflight, and no CORS middleware is
  installed, so the preflight has nothing to succeed against.
- **The `X-LEM-Client` header** (issue #957) — required on every state-changing `/api` request that
  authenticates on the session **cookie**. This is the layer that covers the four query-parameter
  routes below, where the JSON-body layer does not exist.

The third one is a REPLACEMENT, and what it replaces matters. Between #950 and #957 there were only
two: in deployments with `API_ACCESS_TOKENS` set, `/api/*` also needed the bearer token, and a
cross-site form POST cannot set an `Authorization` header even when the attacker knows the value, so
it *did* work here despite being public. It was worthless as access control and real as a CSRF
layer; #950 retired it from the bundle and traded the second for the first. What #950 put in its
place is not a CSRF layer — the `/api` middleware accepts a session credential, and the browser
attaches the cookie by itself — so #957 restored the layer on its own terms rather than on a
credential's.

**"Almost" is exact, and #914 is why.** Four mutating routes take query parameters and no body —
`POST /create_weekly_content/`, `POST /invite_to_li_company_page/`, `POST /aws_test_get_my_profile/`
and `POST /automate_reply_commenting`. Before #914 they authenticated on a `user_id`/`post_id`
parameter, so the cookie was irrelevant to them; now they resolve the session like everything else,
which is what puts them under this heading at all. A cross-site form POST reaches them without a
preflight, so for those four the JSON-body layer is not there — and each of the four only ever
queues work for the CALLER's own account, so the worst a forged one buys is a job the user could
have started themselves. Even so the layer count is what matters, because a new query-parameter
mutating route inherits it: with `X-LEM-Client` it is **two** (`SameSite=Lax` + the header), where
between #950 and #957 it was one.

**And query parameters are not the only shape the JSON-body layer misses.** A cross-origin caller
can also produce `multipart/form-data` without a preflight (a plain `<form enctype=…>`, or
`fetch(…, {mode: 'no-cors', credentials: 'include'})` with a `FormData` body), and two mutating
routes take exactly that — `POST /user/newsletter-draft/cover` and `POST /avatar/training`, both
`Form(...)` + `UploadFile`. `/avatar/training` is the more expensive one: it spends an avatar
credit and starts a LoRA training run. They are covered because the layer was scoped to **every**
state-changing cookie-authenticated request rather than to the four routes that made the gap
visible — which is the whole argument for that scoping, made concrete. Enumerating "the four" as
the complete no-preflight set would have shipped a layer that missed these two.

### `X-LEM-Client` is not a secret

Its value is a constant in a public bundle (`'spa'`, set in `ui/src/api/client.ts`'s one request
interceptor) and it is meant to be. **The mechanism is that a cross-origin HTML form cannot set a
request header at all**, whatever the value would have been, and setting one from `fetch()` needs a
preflight the server answers nothing to. So the server checks that the header is PRESENT and never
what it says — comparing the value would buy nothing against an attacker who can read the bundle,
and would invite the next reader to rotate it like a token and put it in `.env`.

It is the same property the retired bearer token had, which is why #950 took a layer away without
meaning to — but held by a value that is not pretending to be a credential.

Enforcement is in `api/main._require_client_header()`, called from the **one resolver**
(`get_session_user_id`) on its cookie branch, before the scope check and before anything writes —
the same argument as the session-scope narrowing (#905): the credential this defends against is the
one the browser attaches by itself, so the check belongs where that credential is read, not at ~150
call sites. Refusal is **403 `client_header_required`**, never 401, because a 401 is the SPA's
sign-out signal — corroborated since #1358, but still a sign-out, and "your app is stale" is not one.

It applies to **every** state-changing cookie-authenticated `/api` request, not only those four —
`POST`, `PUT`, `PATCH` and `DELETE` alike. The four are what made the gap visible; covering only
them would leave the next query-only route uncovered, which is the failure this exists to stop.

Two deliberate exemptions:

- **Reads.** CSRF is a forged write; with no CORS the attacker cannot read the response, and
  requiring a header on GET would break the browser's own credentialed navigations (a plain
  `<a href>` download, an `<img>` src).
- **A bearer-authenticated caller.** Scripts, Postman and the admin tooling are not browsers and
  have no ambient credential to forge with. This exemption is for NON-BROWSER callers and is
  permanent — it is not a rollout shim to retire once caches turn over, though it happens to cover
  that too: an SPA bundle cached from before #950 still sends a bearer and no header. A request with
  an explicit `session_token` is likewise untouched — the attacker would have to know it, and it is
  httpOnly.

### The two assumptions underneath it

Both are load-bearing, both are pinned by a test, and both are the kind of thing an unrelated PR
could undo in one line:

1. **The SPA is same-origin with the API — by construction, not by deployment.** The axios client's
   `baseURL` is the RELATIVE `/api`, so every request is same-origin whatever the host (Vite dev
   server, docker-compose, the prod nginx edge), and a custom header on a same-origin request is
   never preflighted. An ABSOLUTE `baseURL` would put `X-LEM-Client` behind a CORS preflight the
   server answers nothing to — breaking every request, not just the writes.
   Pinned by `the baseURL is relative` in `ui/src/api/client.test.ts`.
2. **No CORS middleware is installed.** CORS with credentials would let a genuine cross-origin
   caller ask permission to send this header and reinstate the hole the layer closes.
   Pinned by `test_no_cors_middleware_is_installed`.

A third, smaller one: the request and the session cookie are stamped onto their ContextVars by the
SAME middleware block, so a live HTTP request can never carry the cookie without the request. That
is what makes "no request in scope → no-op" safe rather than a silent bypass, and it is why the
no-op logs at DEBUG (it is the expected shape for every Celery/direct caller, and warning on an
expected no-op files a defect for working code). Pinned by
`test_the_request_and_cookie_contextvars_are_set_together`.

### What is NOT covered by this layer, and why that is fine

`POST /auth/logout` is deliberately outside it — it swallows a failed session resolve so that a
logout always completes, which is the right call for a sign-out. Forced logout is a real if
low-severity CSRF target, and what covers it is the JSON-body layer: `LogoutRequest` is a body
model, so a cross-site form (limited to `text/plain`, `application/x-www-form-urlencoded` and
`multipart/form-data`) cannot produce a request FastAPI will accept.

### On the SPA side

A bundle cached from before this shipped sends no header and gets 403 `client_header_required` on
every write. That is a stale bundle, not a dead session, so `ui/src/api/client.ts` routes it into
the existing stale-chunk guard (`recoverFromChunkError`, issue #743): ONE reload lands on a bundle
that sends the header, and a second failure inside the cooldown surfaces "a new version was
released — please refresh" instead of looping. It never signs the user out — that is the 401 branch,
and is exactly why the refusal is a 403.

`tests/unit/api/test_csrf_client_header.py` is the standing proof, with a refused case per each of
the four routes, per unsafe method, and ahead of the scope check; `ui/src/api/client.test.ts` proves
the header rides on every request out of the one axios client and that the 403 reloads rather than
signs out.

**One env var still deletes the other layer.** `_samesite()` accepts `none` (a browser rejects
anything else, so the resolver has to), and `SESSION_COOKIE_SAMESITE=none` attaches the cookie to
cross-site POSTs. Between #950 and #957 that was the whole defence on those four routes; with
`X-LEM-Client` it is one of two again, which is a reason to leave it `lax` rather than a licence to
change it. Nothing in LEM needs otherwise: the cookie is only ever sent to LEM's own origin, and the
one cross-site arrival that matters (the LinkedIn OAuth return trip) is a top-level GET, which `Lax`
already covers.

If a future change adds CORS with credentials, sets `SESSION_COOKIE_SAMESITE=none`, or adds another
form-encoded/query-only mutating endpoint, this section is the thing that has to be revisited first.

## A 401 is about the endpoint, not about the session (issue #1358)

`/api/auth/session` is the ONE route whose 401 answers "am I signed in?". Every other 401 answers
only "this request was not served" — a route that lost its cookie resolution, a scope narrowing, a
handler asking the wrong question. Until #1358 `ui/src/api/client.ts` promoted any of them to a
global verdict: clear `lem_session`, `window.location.href = '/'`.

That is what made #1354 a lockout instead of a broken panel. `/api/dashboard/stats/` and
`/api/activity/` answered **200** throughout, the passkey login itself returned 200 — and the first
401 from a moved route discarded a session the browser had just been handed, then threw away the
client state that would have explained it. Users reported "cannot log in" for most of a working day
and login was never the thing that was broken. In the database every `sessions` row minted during
the incident has `last_seen_at = created_at + 1s`.

Three properties now, in `client.ts` + `contexts/AuthContext.tsx`:

- **Corroborate before tearing down.** A 401 from anywhere else fires ONE `/auth/session` request —
  deduped across a burst, and skipped entirely when no `lem_session` is held, which is also what
  stops a post-teardown burst from re-asking. Only a 401 back ends the session. A 200, a 5xx or a
  network failure leaves it alone: absence of proof that it died is not proof that it died. The
  original error still rejects, so the failing panel surfaces its own failure.
- **The session route's own 401 stays with the auth layer.** `AuthProvider` boots on it (drop the
  stored sentinel) and `login()` answers it by falling back to holding the token — the path that
  exists so a cookie a browser refused is never turned into a lockout. A teardown from underneath
  would break exactly that.
- **No hard redirect.** The teardown is announced as a window event (`SESSION_ENDED_EVENT`,
  `utils/sessionEnd.ts` — the same shape as the chunk-reload notice) and `AuthProvider` performs it
  as a state change. React Router moves the tab to the logged-out tree, client state and error
  boundaries survive, and `sessionEndedReason` gives `LoginModal` something to say. A deliberate
  `logout()` carries no reason — the user knows why that happened.

Proof: `ui/src/api/client.test.ts` (the #1354 shape — session valid, some endpoints 401, some 200,
user stays in; one probe per burst; sign-out once the session route agrees; never a redirect) and
`ui/src/contexts/AuthContext.test.tsx`.

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
| `SESSION_COOKIE_SAMESITE` | `lax` | `strict` breaks the LinkedIn OAuth return trip. **Never `none`** — since #950 `Lax` is the only CSRF defence the four query-parameter mutating routes have, and `none` removes it (see [CSRF](#csrf)) |
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

## The `agent` scope — a credential a machine can hold (issue #1026)

A scheduled headless agent has no way to hold an identity: the `/api` bearer is deliberately not one
(above), the email PIN needs a mailbox, a passkey ceremony cannot be run headless, and a TOTP seed
cannot be re-exported after enrolment. `POST /api/user/agent-token` mirrors the extension token to
close that gap — minted **once, by a human, in the SPA**, behind the same step-up, then held by a
machine. `extension_scope_ok` is deliberately NOT passed to `_require_step_up`: a token that could
mint its successor would never need a human again.

`_AGENT_SESSION_SURFACE` is the list. It reaches the review queues, the settings that decide whether
queueing is safe (`/user/engagement-preferences`, `/user/automation-status`, `/dashboard/stats`) and
the create routes. It reaches **no** credential path, **not** the account mover, **not** session
revocation, and **neither minting endpoint** — a stolen agent token cannot mint its successor or
lock the owner out.

### An agent may queue work; only a human may approve it

This **cannot** be a path list — saving a draft and approving one are the same `PUT` — so it is
enforced server-side on the fields that turn a draft into a send. It took three guards, because
there are three ways a row reaches APPROVED and only the first one says "approve":

| Route shape | Reaches APPROVED via | Guard |
|---|---|---|
| the five PUTs (`/dm`, `/connection_request`, `/outreach/target`, `/lead_signal`, `/catchup/touch`) | `action="approve"` | `_refuse_agent_approval` |
| the creates (`/schedule_dm`, `/connection_request`, `/outreach/target`) | `status="approved"` at insert time — no `action` field anywhere | `_refuse_agent_approved_status` |
| `POST /connection_request` with no status at all | the account's `connection_request_mode`, which is `auto_approve` **out of the box** | `_agent_scoped()` in the handler |

Guarding only the first left the other two open, and the second is the one that matters most: a
`POST /schedule_dm` carrying `status="approved"` lands a row `auto_check_scheduled_dms` then **sends
to a real person**. The third is worse in a quieter way — it is the guarantee broken by the agent
doing nothing at all, on nearly every account, because the account default is permissive.

**`PUT /user/engagement-preferences` is refused for an agent** (403 `agent_may_not_configure`; the
read still works). The scope surface matches on PATH, not method, so the entry added so the agent
could *read* whether automation was safe granted the write along with it — and that write sets the
approval modes above and every per-day cap. A token that cannot approve one item must not be able to
configure the account into approving all of them. **This is the general hazard of a path-scoped
surface: granting a path grants every method on it.** Check the verbs before adding an entry.

### The TTL is fixed at mint, and never slid

`create_session(ttl_hours=...)` grants an agent session a long life (`ttl_days`, default 90) because
a weekly agent would find a 24h idle session dead every run. That parameter alone was not enough:
`resolve_session` slides EVERY session to `now + SESSION_IDLE_HOURS`, so the 90-day token was
rewritten to 24 hours on its **first request** — the same failure, now with a parameter that looked
like it had fixed it.

So an `agent` session is the one scope whose expiry never slides. Sliding it by the granted TTL
instead would have been worse: a machine calling on a schedule renews forever, making the credential
with the widest time window the only one with no ceiling. A fixed expiry is a real deadline that only
a human ceremony can extend — which is also why `SESSION_ABSOLUTE_MAX_DAYS` is not applied to it, as
that cap exists to bound a sliding window and there is none to bound. The row stays an ordinary,
per-device revocable session on the Security card, so a long TTL is not a one-way door.

`sessions.scope` is `VARCHAR(32)` and `auth_audit_log.event` is `VARCHAR(50)`, so `agent` and
`agent_token_minted` needed **no migration** — an ENUM would have.

## The docs surface lives inside `/api` (issue #1020)

At the FastAPI defaults the docs surface sits at `/docs`, `/redoc` and `/openapi.json` — **outside**
`/api`, so the credential gate (which only inspects paths starting with `/api/`) never saw it and
all three were served to anyone. The app now declares `docs_url="/api/docs"`,
`redoc_url="/api/redoc"`, `openapi_url="/api/openapi.json"`, which puts them on the same side of
that boundary as everything they describe; they are then re-opened **deliberately** as leaf entries
in `_PUBLIC_API_PREFIXES` rather than by accident of routing.

Three details are load-bearing:

- **`swagger_ui_oauth2_redirect_url="/api/docs/oauth2-redirect"` must be set explicitly.** FastAPI
  defaults it to the literal `/docs/oauth2-redirect` and does NOT derive it from `docs_url`, so
  moving `docs_url` alone strands the helper outside `/api` and breaks Swagger's Authorize flow
  silently.
- **The old paths 301 to the new ones** (`_DOCS_REDIRECTS`), registered before the SPA catch-all —
  which would otherwise answer them with `index.html`. Permanent, matching the `/assets` redirect,
  so bookmarks, README links and Postman imports keep working.
- **`_hide_admin_routes_from_schema()` keeps every `/api/admin/*` operation out of the published
  schema.** It walks the route table and flips `include_in_schema`, rather than decorating eighteen
  routes with `include_in_schema=False`, because that failure mode is silent: a nineteenth admin
  route added later would publish itself and nothing would say so. The count is logged
  (`_ADMIN_ROUTES_HIDDEN`) so "nothing to hide" reads differently from "the walk matched nothing",
  and `test_no_admin_route_appears_in_the_public_schema` checks the outcome.

**Hidden is not gated.** The admin routes' auth is exactly what it was — `X-Admin-Secret` and, for
the engagement test routes, the bearer alongside it. What changed is that the public schema no
longer hands a prober the list of paths to aim at, and that Swagger's *Try it out* can no longer
drive them. Use curl or the checked-in Postman collection instead:
[`TESTING_ENGAGEMENT_API.md`](TESTING_ENGAGEMENT_API.md).

### What the published schema says a route RETURNS (issue #1219)

Every route answers with the same envelope, `ResponseModel` in `api/models.py`. Its `detail` was
`Any`, which is exactly nothing to a reader of a now-public schema: 130-odd operations all documented
the same empty object. `ResponseModel` is now generic, and each handler's return annotation names the
payload it actually produces — `ResponseModel[str]` for a message, `ResponseModel[dict[str, Any]]`
for an object, `ResponseModel[list[dict[str, Any]]]` for an array, `Optional[...]` where `None` is a
real answer ("nothing is queued") rather than an error.

Two properties are deliberate:

- **The parameter is a CONTAINER type, never a per-route field model.** FastAPI serializes the
  response *through* the annotation, so a model with named fields would silently DROP every key it
  does not declare — a documentation change that quietly deletes data the SPA reads. The container
  types narrow the schema while leaving the response bytes identical, which is why the OpenAPI diff
  for #1219 is `$ref` changes and nothing else.
- **Bare `ResponseModel` is still valid and still means `Any`.** Pydantic treats an unparametrized
  type variable as `Any`, so nothing had to change at once and a caller constructing the envelope by
  hand is unaffected. What catches a new un-annotated route is
  `test_no_operation_still_points_at_the_any_envelope`: the bare component is no longer referenced by
  any operation, so re-introducing it fails. `ResponseModel[Any]` is the escape hatch — exempted by
  name in that test file — and it says "this route genuinely has no one shape" on purpose.

The annotation is a runtime contract, not a comment: `ResponseModel[str]` returning a dict is a 500,
not a lax response. `test_no_literal_return_contradicts_its_annotation` reads the handlers' literal
`detail=` returns back out of the source, so that drift fails in the unit lane instead of on the
branch nobody exercised.

#### The SPA's types are GENERATED from that schema (issue #1446)

The published document is now the source of the SPA's response types, so a route and the browser
code reading it cannot drift silently. Three artefacts, all committed:

| Artefact | Written by | Read by |
|---|---|---|
| `src/cqc_lem/ui/openapi.json` | `poetry run python scripts/dump_openapi.py` | the generator |
| `src/cqc_lem/ui/src/api/schema.ts` (+ `schema.stamp.json`) | `npm run gen:api-types` (openapi-typescript, pinned in `package.json`) | the SPA |
| `src/cqc_lem/ui/src/api/types.ts` | a human, once | every call site |

**After changing any route's shape, run both commands and commit all four.** The dump imports the
app directly — no server, no database — and takes `app.openapi()` *after*
`_hide_admin_routes_from_schema()` has run, so `/api/admin/*` stays out of a file that ships to the
browser as types.

`tests/unit/api/test_openapi_snapshot.py` holds the whole chain from the REQUIRED Unit Tests lane.
App → snapshot is exact (the bytes are re-rendered and compared). Snapshot → TypeScript is checked
through `schema.stamp.json`, the hash of the schema the generator last ran on — that lane has no
node, so it cannot re-run the generator, and the stamp is what makes "regenerated one, forgot the
other" fail there anyway. `npm run check:api-types` re-runs the generator and diffs, which is the
exact answer wherever node is available.

`api/types.ts` is the only hand-written piece: `ApiEnvelope<T>` takes `status_code` from the
generated envelope component, and `GetDetail<'/api/…'>` picks a documented payload out of `paths`.

**Narrowing a payload beyond `Record<string, unknown>`** — which is where a `dict[str, Any]`
`detail` lands — is a documentation-only change, never a serialization one. Give the route
`responses={200: {"model": ResponseModel[X]}}` with `X` from `api/response_schemas.py`: FastAPI
uses it for the schema and keeps serializing through the return annotation, so every key the
handler returns is still on the wire, including ones no model declares. The rule that makes it safe
is that the model must be *derived* from what the handler really returns — the stored columns, or
the literal dict in the source — which is what `tests/unit/api/test_response_schemas.py` proves for
each one. A field documented but not returned is worse than an undocumented one: the SPA generates a
type from it and reads `undefined`. The same rule decides REQUIRED: `= None` on a model field
documents "may be absent" (`key?:` in the generated TypeScript), not "may be null" — so a key the
handler always writes is `Optional[X]` with no default, and only a genuinely partial record (the
Redis-backed ones, `extra="allow"`) keeps its defaults. On the payloads a PUT writes back whole, a
key the SPA is allowed to omit is a column a partial save resets.

Every payload the SPA reads is narrowed this way as of issue #1538 — the nine account-page reads
(engagement targets, story bank, DM templates, newsletter settings/subscribers/draft, groups, group
post draft, lead magnet) plus the dashboard's counters, planned tasks and activity feed and the
Content Studio's post page. Nothing in `ui/src/pages/account/types.ts` that names an endpoint
payload is hand-written any more; what stays there is the constants (`WEEKDAYS`,
`TARGET_CATEGORIES`, `STORY_KINDS`, `DM_EVENTS`, `CATCHUP_EVENTS`, `csv`/`parseCsv`) and one
`Editable<Row, Written>` helper. That helper exists because two of those payloads are also EDITED:
the roster and story-bank cards build rows that have never been saved, so the automation-owned
columns (`id`, the rotation counters, the follow/connect ladder) do not exist on them yet. It keeps
exactly the fields the matching PUT writes required and lets the rest be absent — every name and
type still coming from the schema. Widening one to a plain `Partial` would reintroduce the original
bug: the PUT replaces the whole row, so a droppable editable field is a column a save resets.

A `Literal[...]` in a response model is a second copy of a MySQL ENUM or a `StrEnum`, and it earns
that by giving the SPA a real union instead of `string` (the roster's badge logic is checked at
compile time off it). `TestTheDocumentedVocabulariesAreTheStoredOnes` pins every one of them against
the tuple or enum the writer uses, so the copy cannot drift. Where the server does NOT enforce a
vocabulary — the newsletter `cadence`, a post `status` — the model stays `str`, because documenting
a closed set nothing closes is the same kind of lie as documenting a field nothing returns.

The other unauthenticated surface, `GET /health/deep`, was trimmed in the same issue: it returns
**counts only** — no worker or queue names — and `"status":"healthy"` stays the first key of the
response, which is a monitor contract. See [`stack-watchdog.md`](stack-watchdog.md).
