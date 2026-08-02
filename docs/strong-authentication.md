# Strong authentication — passkeys, TOTP, recovery codes, step-up (issue #745, PR 2c)

The design, threat model and the rest of Phase 2 live in [`AUTH_SECURITY_DESIGN.md`](AUTH_SECURITY_DESIGN.md);
[`secrets-at-rest.md`](secrets-at-rest.md) is 2a and [`identity-and-sessions.md`](identity-and-sessions.md)
is 2b. This file is the **operator + reviewer** half of 2c: what a user now proves to sign in, what
a session has to prove before it may touch a LinkedIn credential, and what happens when a device is
lost.

## What changed

| Before (2b) | After (2c) |
|---|---|
| an emailed 6-digit PIN WAS the login | the PIN is a **bootstrap**: for an account with a strong factor it proves the mailbox and nothing else |
| nothing was phishing-resistant | **passkeys** — origin-bound, so a proxy page cannot relay them |
| losing the mailbox lost (or leaked) the account | **recovery codes** — single-use, argon2id-hashed, shown once |
| a stolen session could write LinkedIn credentials | **step-up**: those writes need a factor proved in the last 5 minutes |
| the Security card listed devices | it also enrols, lists and removes factors |

## The prerequisite, and how it was checked

WebAuthn only runs in a secure context, so 2c was gated on the public hostname serving a valid
certificate. Verified before the work started:

```
$ openssl s_client -connect lem.christopherqueenconsulting.com:443 \
    -servername lem.christopherqueenconsulting.com | openssl x509 -noout -subject -issuer -dates -ext subjectAltName
subject=CN = christopherqueenconsulting.com
issuer=C = US, O = Google Trust Services, CN = WE1
notAfter=Oct  4 01:13:30 2026 GMT
X509v3 Subject Alternative Name: DNS:christopherqueenconsulting.com, DNS:*.christopherqueenconsulting.com
Verify return code: 0 (ok)
```

The wildcard covers `lem.` and the chain verifies, so the RP id `lem.christopherqueenconsulting.com`
is registrable. `webauthn_util.relying_party()` re-checks the *shape* of this at call time and
refuses (`WebAuthnUnavailable` → HTTP 503, passkey UI hidden) rather than offering a button that
cannot work.

## The three factors

`utilities/auth_factors.py` is the ONE place a user's factor state is decided — the login path, the
step-up gate and the Security card all read the same functions, so what a user is told and what the
server enforces cannot disagree.

- **Passkey** (`utilities/webauthn_util.py`, `webauthn` / py_webauthn). The only phishing-resistant
  option, and the only login that mints a session already stepped up. Registration is
  username-less-capable (`resident_key=preferred`) so sign-in needs no email at all — which also
  means `/auth/passkey/login/begin` cannot be probed for whether an address has an account.
  `excludeCredentials` stops the same authenticator being enrolled twice and believed to be a spare.
- **TOTP** (`pyotp`). Not phishing-resistant — it defeats a compromised mailbox, not a proxy page —
  but it is the path for people passkeys do not fit. The seed is stored as a `lemv1:` envelope
  (2a's `crypto.py`) bound to the user, so a DB dump yields no working authenticator.
- **Recovery codes** (`argon2-cffi`). Ten codes, base32 minus the characters people mistype off a
  printed sheet, shown exactly once. They sign you in and let you enrol a new factor; they do **not**
  step a session up, so a found sheet is not a LinkedIn session.

### Counters, and why both kinds share one column

`user_auth_factors.sign_count` is the factor's monotonic counter for both kinds: a passkey's WebAuthn
signature count and an authenticator app's last accepted TOTP time step. Each must strictly increase.
That is what makes a **cloned authenticator** (a counter that went backwards — py_webauthn raises)
and a **re-typed TOTP code** fail rather than pass. The TOTP guard matters more than it looks: a code
is valid across a ±1 step drift window, i.e. up to 90 seconds, which is ample time for a phishing
proxy to relay it a second time. TOTP cannot be made phishing-resistant, but it can be made
single-use.

## Signing in

```
                            ┌── no strong factor ──→ session (not stepped up)
email + PIN ── PIN ok ──────┤
                            └── strong factor ─────→ pending handle + methods
                                                        ├── TOTP code      → session, STEPPED UP
                                                        └── recovery code  → session, NOT stepped up
passkey assertion ────────────────────────────────→ session, STEPPED UP
```

The **no-mail-provider bypass** (`/auth/email/init` with no provider configured) takes the same
second-factor branch. It skips the PIN entirely, so it is the weakest way in — leaving it ungated
would have been a hole straight through 2c on any deployment with mail unconfigured.

`email_verified_at` is still stamped when the PIN is consumed, before the second-factor branch: the
mailbox proved the address whether or not a factor follows.

## The step-up gate

`sessions.last_verified_at` is when THIS session last proved a strong factor.
`auth_factors.step_up_satisfied()` gates every credential-touching endpoint:

| Endpoint | Why it is gated |
|---|---|
| `POST /api/user/linkedin-cookie` | storing a `li_at` IS handing over a LinkedIn session |
| `PUT /api/user/linkedin-password` | the worst single item in the database (design §1) |
| `POST /api/user/email/change/init` | taking the address is how an account is stolen for good |
| `POST /api/user/sessions/revoke` | locking the real owner out of their own account |
| `POST /api/user/extension-token` | mints a token that may later store a cookie |
| `POST /api/user/auth-factors/delete` | removing the thing that protects the account |
| `POST /api/user/recovery-codes/regenerate` | a new sheet silently invalidates the old one |

Four ways to pass, each deliberate:

1. **`STRONG_AUTH_ENABLED=false`** — the kill switch, 2b behaviour, enrolled factors untouched.
2. **The account holds no strong factor.** There is nothing to prove with. The rollout is opt-in
   first (design §7 Stage 2); gating these accounts would brick the cookie paste and the email
   change for everyone who has not enrolled. The gate is what makes enrolment worth something — it
   is not a retroactive lock, and this is the honest limit of 2c until enrolment is mandatory.
3. **The session proved a factor** inside `STEP_UP_MAX_AGE_MINUTES`.
4. **The caller is the browser extension's own session** — and only at the ONE endpoint that opts
   in. See below.

### Adding a factor — the FIRST one is free, the rest are gated

`auth_factors.enrollment_allowed()` is a separate verdict from `step_up_satisfied()` and it has to
be, because the two obvious answers are both wrong:

- **Wide open** hands a stolen session a step-up it never proved. Enrolling stamps the session as
  verified (otherwise a user who just touched their sensor would be asked to touch it again to save
  their recovery codes), so an ungated enrolment is: XSS → add my own passkey → stamped → read the
  `li_at`. That is threat **T4**, and it would walk straight through the gate next to it.
- **Always gated** means an account with nothing enrolled can never enrol anything.

So the line is drawn at what the account already holds:

| Session | May add a factor? |
|---|---|
| account holds no confirmed factor | yes — nothing to prove with, and this is the bootstrap |
| proved a factor inside the freshness window | yes |
| ordinary session, stale or never verified | **no** — 403 `step_up_required` |
| signed in with a **recovery code** (`sessions.scope='recovery'`) | yes, and it is **not stamped** |

Gating the second factor creates no lockout, and that is the whole reason it is safe: every way to
sign in to an account that already holds a factor either arrives already stepped up (passkey login,
PIN + TOTP) or is a recovery-code session, which is let through by name. The person §6.8 worries
about — someone who lost the factor they had — comes back on a recovery code and enrols a
replacement, exactly as designed.

A recovery-code session enrolling does **not** get stamped for doing so. It runs the ordinary
step-up ceremony with the factor it just enrolled: one extra touch, and an audited
`STEP_UP_VERIFIED` row rather than a silent promotion. Be honest about what this does and does not
buy — a recovery sheet is an account-recovery credential, so someone holding one can ultimately
reach the LinkedIn credentials by enrolling their own factor first. What it cannot do is reach them
*directly*, in one step, with nothing on the audit trail.

**Removing a factor always needs step-up**, and one authenticator app is the maximum: starting a
TOTP enrolment while a confirmed one exists is a 400, because a second confirmed row would count
towards `has_strong_factor` and show on the Security card while only the newer seed's codes were
ever checked.

A refusal is **403** with `{"code": "step_up_required", "methods": [...]}`, never 401: the SPA's
axios interceptor treats any 401 as a dead session and signs the user out, so answering "prove it's
you" with a 401 would log them out instead of asking them anything. The browser side is
`hooks/useStepUp.tsx` — `guard()` wraps a call, opens the modal on that 403, and re-runs the
original call once a factor is proved. Callers write one line and never learn the gate exists.

### The browser extension (design §6.5, "decide this in 2c")

`/api/user/linkedin-cookie` is deliberately bearer-exempt because the LinkedIn Connect extension
POSTs to it holding only a LEM session token. It can never run a WebAuthn ceremony, so a naive gate
would break the one-click reconnect and push users back onto stored passwords.

The resolution: **the extension's step-up happens once, in the SPA, when its token is minted.**
`POST /api/user/extension-token` is itself step-up gated, and it mints a session row with
`scope='extension'`. The token is listed and revocable beside every other device on the Security
card, so the right to store a cookie can be withdrawn without signing the person out of the app.

The scope exemption is **opt-in per call site** (`step_up_satisfied(..., extension_scope_ok=True)`)
and exactly one site opts in — `/api/user/linkedin-cookie`. That is load-bearing, not tidiness: an
extension token is otherwise an ordinary session, so a blanket exemption would let a stolen one
change the email address and revoke every device, which is the precise escalation the gate exists to
stop. If a future endpoint needs the extension to reach it, it has to say so explicitly.

**Since 2c.1 (issue #905) the scope is also a SURFACE, not just a step-up exemption.** An extension
token used to be an ordinary full session that merely *also* satisfied the gate at the cookie
endpoint — it could read every post, DM template and setting the SPA can. It now reaches exactly one
path, `/api/user/linkedin-cookie`, and anything else is a **403** `session_scope_forbidden`.

**Honest limit, and it is not small:** the narrowing binds every route that authenticates from the
SESSION, which is all of them bar one group. A set of `/api` endpoints predating 2a still identify
the user from an `email` / `user_id` **request parameter** (`PUT /user/`, `GET /posts/`,
`GET /dashboard/stats/`, …) behind nothing but the shared bearer token the SPA ships in its build.
Those never call the resolver, so no session scope — extension, enrolment, or any future one —
constrains them. That is tracked as **#914** and is the ceiling on what this section can claim
until it lands.

The narrowing is enforced in `api/main.get_session_user_id` — the ONE resolver every handler already
calls — and not at ~150 call sites, because a narrowing that has to be remembered per endpoint is a
narrowing that leaks. `_EXTENSION_SESSION_SURFACE` is the list; adding an entry to it hands every
extension token, including a stolen one, whatever that endpoint can do. The per-call-site
`extension_scope_ok` flag stays as it is: a future surface entry must not arrive with a step-up
exemption already attached.

**A refusal on the extension scope writes an `auth_audit_log` row** (`session_scope_denied`, with
the client and the path) **and logs a WARNING**. The extension calls one endpoint, so that row
cannot appear by accident — it is the clearest signal available that someone else is holding the
token, and it is worth chasing. A held *enrolment* session deliberately writes nothing and logs at
DEBUG: those refusals are constant and harmless while the SPA settles, warning on an expected no-op
would file a defect for working behaviour, and auditing them would bury the one row that means
something.

**Which scopes reach everything is an explicit list, not the absence of an entry.**
`_UNRESTRICTED_SCOPES` names `full` and `recovery`; a legacy `NULL` row (every session written
before 2c) resolves to `full` before the check and is untouched. Anything else the surface table
does not recognise — a typo, a hand-edited row, a scope some later phase adds and only half wires
up — is **refused**, not waved through. Deriving "unrestricted" from "has no surface entry" would
have made the table itself the opt-in thing this whole design exists to remove.

**Three drift guards, because both surfaces are path literals.** `tests/unit/api/test_session_scopes.py`
asserts every surface entry is a route the router actually serves — rename a route and the `enroll`
surface silently becomes a lockout, since the gate's own fetch would 403 — and asserts the extension
surface EQUALS the set of `/api` paths `browser_extension/popup.js` fetches, in both directions: a
path the extension calls but the surface omits breaks the reconnect click, and a surface entry the
extension never calls is blast radius handed to a stolen token for nothing. The third covers the
axis a path literal cannot express: **a surface entry is method-blind**, so adding
`GET /user/linkedin-cookie` later would hand every extension token — including a stolen one — a READ
of the LinkedIn cookie, with nothing in the source saying so. The guard pins that entry to `POST`
alone, so widening it has to be a deliberate edit to a test that explains why.

**The hold is decided in one place on the minting side too.** `_mint_login_session` is what every
PIN login path calls; the strong-factor login paths (`/auth/passkey/login/complete`,
`/auth/second-factor/verify`) deliberately do not, because reaching either one proves the account
holds a factor and `enrollment_required` is false for them by construction.

## Mandatory enrolment (2c.1, design §7 Stage 2)

2c shipped the first half of Stage 2 — an account that HAS a factor can no longer sign in on a PIN
alone. `REQUIRE_STRONG_FACTOR_AFTER` is the second: after that date, an account with **no** factor
stops getting an ordinary session too.

| `REQUIRE_STRONG_FACTOR_AFTER` | PIN login for a factor-less account |
|---|---|
| empty (the default, and every deployment today) | a full session — 2c behaviour, unchanged |
| a future date | a full session, plus a dismissible SPA prompt naming the date |
| a past date | a session scoped **`enroll`** — signed in, and held |

A held session is **not a lockout**, and that distinction is the whole design. The PIN still signs
the account in; what it no longer hands over is a session that can do anything. `scope='enroll'`
reaches `_ENROLL_SESSION_SURFACE` — who am I, sign me out, the SPA's boot payloads, and the
enrolment ceremonies — and nothing else, with a **403** `enrollment_required` everywhere else. The
SPA reads that state off `/auth/session` and renders `StrongFactorGate` *instead of* the app rather
than over it, because every page behind it would 403 anyway. It reads it off the SAME resolve that
authenticated the request (a ContextVar the resolver stamps), so the browser can never be told
"not held" by a page whose every request is then refused.

**The hold belongs to the ACCOUNT, not to the session row**, and that is the subtle half. The row
records it, but `_scope_checked` re-asks `enrollment_required(user_id)` and releases the moment the
answer is no. Deciding it from the row alone is a dead end on every *other* device: enrol on the
laptop and the phone still holds a row saying `enroll`, while the account now HAS a factor — so
enrolling again is step-up gated, and the step-up ceremony is deliberately outside the enrolment
surface. The same re-ask is what makes the rollback real and what survives a promotion that failed
to write.

When a passkey or authenticator lands, `release_enrollment_scope` promotes the row to `full` in the
same request (a conditional `UPDATE ... WHERE scope='enroll'`, so a full, recovery or extension
session enrolling a factor is never widened by it). That write is bookkeeping — it saves the
re-ask, it does not grant the access. Recovery codes are inside the surface too: being forced to
enrol and then unable to save the sheet would be the worst possible order.

Two operator notes:

- **The date is read at the CALL SITE**, not captured at import, and every read of a held session
  re-asks it. So clearing the variable, moving it forward, or setting `STRONG_AUTH_ENABLED=false`
  releases everyone *already* held — the rollback does not strand the people who signed in during
  the window until their sessions expire.
- **An unparseable date is treated as unset and WARNS** (once per distinct bad value per process —
  the parse is not cached, the warning is, or one typo would put a WARNING on every session check).
  Failing the other way would force every user in the deployment into enrolment over that typo.
- **A signup that lands after the date is held too**, before onboarding. That is deliberate: a new
  account is asked for LinkedIn credentials almost immediately, which is precisely what the factor
  is there to protect.

Nobody meets the deadline cold: `StrongFactorPrompt` appears as soon as a date is scheduled, says
when, and links to the Security card. It is dismissible in the browser only — enrolling is what ends
it for good, because the server stops sending `strong_factor_prompt` the moment a factor exists.

**A dismissal is stored against the NOTICE it dismissed, not as a permanent "seen it"**, or the
claim above would not survive its first `Not now`. Two things are new information and neither may be
swallowed by a click from weeks ago: the operator **moving** the date — bringing it forward is the
dangerous direction, since the person was told a later one — and the date **arriving**, which
changes the message from "from `<date>`" to "from your next sign-in". Each is a different notice, so
each is shown once more.

## Ceremony state

`auth_challenges` holds what is in flight: the WebAuthn challenge an authenticator must sign, and
the pending handle issued when a PIN succeeded but a factor is still owed. It is **MySQL, not
Redis** — Redis holds runtime state that is allowed to fail open (pacing seeds, the auth rate
limiter), and a challenge store that failed open would let a replayed assertion through. The handle
is returned to the caller and the row stores its SHA-256, the same posture as a session token, and
it is claimed by an `UPDATE ... WHERE consumed_at IS NULL` so two replays cannot both win.

**A pending handle survives a wrong code and is burned by the last allowed one.**
`/auth/second-factor/verify` goes through `claim_auth_challenge_attempt`, which counts the attempt
and sets `consumed_at` in the same statement once `SECOND_FACTOR_MAX_ATTEMPTS` (default 5) is
reached — so two concurrent guesses cannot both be the last one. `auth_challenges.attempts` is the
DURABLE bound on guessing: 5 tries out of a million is not an oracle, and unlike the per-email/per-IP
limiter in front of it (Redis, **fails open**) it does not disappear when Redis does.

Consuming on first touch would read as safer and is not: one mistyped digit would end a login whose
only way back is the whole email round trip, on the single path that has no way around it. Set
`SECOND_FACTOR_MAX_ATTEMPTS=1` to get that behaviour back.

**The budget is per ACCOUNT, not per handle** (`SECOND_FACTOR_ATTEMPT_WINDOW_MINUTES`, default 15).
A per-handle counter bounds one pending login and nothing else, because the stage in front of it
hands out a new handle — and a new counter — for free, and both ways to reach that stage are inside
the threat model: the no-mail-provider bypass needs no proof at all, and a compromised mailbox (T2)
can mint PINs all day. Five guesses per round with unlimited rounds walks a 6-digit space. So
`_begin_second_factor` sums the account's attempts over the window, refuses with **429** once the
budget is gone, and seeds the new handle with what was already spent; a correct code clears the
count outright (`clear_challenge_attempts`), so a user who mistyped once is not carrying it into
their next sign-in. An unreadable count fails **closed** — a database that will not answer is not
an empty budget.

The shape of the refusal is load-bearing for the SPA: **401 means the code was wrong and the pending
sign-in is still alive** (stay on the field), **400 means the handle is gone** — expired or out of
attempts — so `LoginModal` sends the user back to the email step instead of retyping into a login
that no longer exists. `clear_auth_limits` runs when the PIN validates, so the first stage's failed
attempts can never throttle the second — and it deliberately does **not** run on the
no-mail-provider bypass, where nothing was proved and the only counters it could clear are an
attacker's own.

## Schema

`V20260802020439__strong_auth_phase_2c.sql` — additive, nothing backfilled:

- `user_auth_factors` — one row per passkey/TOTP factor. `credential_id_hash` carries the UNIQUE
  index because a raw credential id is up to 1023 bytes and a truncated-prefix unique key would
  reject a legitimate authenticator.
- `user_recovery_codes` — argon2id hashes; used rows are kept so the page can say "3 of 10 left".
- `auth_challenges` — ceremonies in flight. `attempts` is the durable guessing bound, carried across
  handles so it bounds the account and not just one pending login.
- `sessions.last_verified_at`, `sessions.scope` (`full` / `extension` / `recovery` / `enroll`).
  `scope` is a `VARCHAR(32)`, so 2c.1 added `enroll` with **no migration** — an ENUM would have
  needed one.

## Environment

| Variable | Default | Why you'd change it |
|---|---|---|
| `WEBAUTHN_RP_ID` | derived from `PUBLIC_BASE_URL` | split-host setup only |
| `WEBAUTHN_RP_NAME` | `LinkedIn Engagement Manager` | what the authenticator shows |
| `WEBAUTHN_EXTRA_ORIGINS` | empty | `http://localhost:5173` for local dev |
| `STEP_UP_MAX_AGE_MINUTES` | `5` | longer is friendlier and weaker |
| `AUTH_CHALLENGE_TTL_SECONDS` | `300` | how long a ceremony may sit half-finished |
| `RECOVERY_CODE_COUNT` | `10` | size of the sheet |
| `SECOND_FACTOR_MAX_ATTEMPTS` | `5` | `1` = the handle dies on one wrong code |
| `SECOND_FACTOR_ATTEMPT_WINDOW_MINUTES` | `15` | how long a spent guessing budget is remembered |
| `STRONG_AUTH_ENABLED` | `true` | `false` rolls 2c back without deleting a factor |
| `REQUIRE_STRONG_FACTOR_AFTER` | empty (never) | the date mandatory enrolment starts — see above |

## When a user loses everything

T5 (root on the VPS) is out of scope; support tickets are not. If someone loses every enrolled
factor **and** their recovery sheet, there is no self-serve path back — that is the point of the
design, not a gap in it. The operator path is deliberately manual and deliberately at the database,
so it leaves a trace and cannot be triggered by anything reaching the app:

1. Verify the person out of band. A mailbox is not proof here — the whole premise of 2c is that a
   mailbox alone no longer signs this account in.
2. Delete their `user_auth_factors` rows. The account drops to zero factors, `has_strong_factor()`
   goes false, and the email PIN is a full login again exactly as it was in 2b.
3. Tell them to enrol a passkey and save a new sheet on that first sign-in — step 2 left the account
   with no strong factor at all.

Rotating just the recovery sheet (`POST /user/recovery-codes/regenerate`) is the lighter case and
needs no operator: it is step-up gated, so it only works for someone who still holds a factor.

## Still open after 2c.1

- **Nobody has scheduled the deadline yet.** `REQUIRE_STRONG_FACTOR_AFTER` is empty in every
  deployment, so mandatory enrolment is built and off. Until a date is set, an account with no
  factor still signs in on a PIN and still passes the step-up gate — the 2b posture, by choice.
  Setting it is an operator decision, not a code change.
- **`users.password` still exists.** 2a encrypted it and made cookie-only the default; dropping the
  column waits until the prompt has drained the remaining password-only accounts (design §5.4).

Closed by 2c.1 (issue #905): mandatory enrolment now exists behind that date, and `extension`-scoped
sessions are restricted to the one endpoint the extension calls.
