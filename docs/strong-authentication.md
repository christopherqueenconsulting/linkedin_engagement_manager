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

**Adding a factor is never gated; removing one always is.** A recovery code exists so you can enrol
a new passkey after losing the old one — gating enrolment behind a factor you no longer hold is a
lockout, not a control.

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

Known limit, worth naming: an `extension`-scoped session is otherwise a full session for everything
that is NOT step-up gated, exactly as it was in 2b. Narrowing it to the cookie endpoint alone is a
follow-up, not a 2c regression.

## Ceremony state

`auth_challenges` holds what is in flight: the WebAuthn challenge an authenticator must sign, and
the pending handle issued when a PIN succeeded but a factor is still owed. It is **MySQL, not
Redis** — Redis holds runtime state that is allowed to fail open (pacing seeds, the auth rate
limiter), and a challenge store that failed open would let a replayed assertion through. The handle
is returned to the caller and the row stores its SHA-256, the same posture as a session token, and
it is claimed by an `UPDATE ... WHERE consumed_at IS NULL` so two replays cannot both win.

**A pending handle is spent by the attempt, not by the success** — `/auth/second-factor/verify`
consumes it before it looks at the code, so a wrong TOTP or recovery code ends that sign-in and the
user starts again from the email PIN. That is the brute-force bound on the second stage: the 6-digit
space is never walked, because there is exactly one guess per handle. It is a real UX cliff (a
mistyped digit costs a whole login), and it is the deliberate trade — the alternative, a handle that
survives wrong codes, is an unauthenticated 6-digit oracle. The stage is additionally bounded by the
per-email/per-IP auth limiter, and `clear_auth_limits` runs when the PIN validates so the first
stage's failed attempts can never throttle the second.

## Schema

`V20260802020439__strong_auth_phase_2c.sql` — additive, nothing backfilled:

- `user_auth_factors` — one row per passkey/TOTP factor. `credential_id_hash` carries the UNIQUE
  index because a raw credential id is up to 1023 bytes and a truncated-prefix unique key would
  reject a legitimate authenticator.
- `user_recovery_codes` — argon2id hashes; used rows are kept so the page can say "3 of 10 left".
- `auth_challenges` — ceremonies in flight.
- `sessions.last_verified_at`, `sessions.scope`.

## Environment

| Variable | Default | Why you'd change it |
|---|---|---|
| `WEBAUTHN_RP_ID` | derived from `PUBLIC_BASE_URL` | split-host setup only |
| `WEBAUTHN_RP_NAME` | `LinkedIn Engagement Manager` | what the authenticator shows |
| `WEBAUTHN_EXTRA_ORIGINS` | empty | `http://localhost:5173` for local dev |
| `STEP_UP_MAX_AGE_MINUTES` | `5` | longer is friendlier and weaker |
| `AUTH_CHALLENGE_TTL_SECONDS` | `300` | how long a ceremony may sit half-finished |
| `RECOVERY_CODE_COUNT` | `10` | size of the sheet |
| `STRONG_AUTH_ENABLED` | `true` | `false` rolls 2c back without deleting a factor |

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

## Still open after 2c

- **Enrolment is opt-in.** Design §7 Stage 2 has a `REQUIRE_STRONG_FACTOR_AFTER` date, after which
  enrolment becomes mandatory at next login. Until then, an account with no factor passes the
  step-up gate — the same posture it had in 2b.
- **`users.password` still exists.** 2a encrypted it and made cookie-only the default; dropping the
  column waits until the prompt has drained the remaining password-only accounts (design §5.4).
- **`extension`-scoped sessions are not otherwise restricted** — see above.
