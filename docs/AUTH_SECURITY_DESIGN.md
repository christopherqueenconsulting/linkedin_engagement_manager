# Auth, Identity & Session-Secret Protection — Research & Recommended Design

**Issue:** #568 — Phase 1 (research). **Status:** ✅ **Approved by @gitchrisqueen on 2026-07-25** (PR #575,
answers `1A 2A 3A`). Phase 2 builds exactly what §10 records.
**Constraints:** security-first / phishing-resistant, easy to operate, **no paid third-party services**
(self-hosted, free-forever libraries only).

All file references below are repo-relative and were verified against the tree at the time of writing.

---

## 0. Approved decisions (owner sign-off)

| # | Question | Decision | Where it is specified |
|---|---|---|---|
| 1 | Primary authentication method | **1A — Passkeys/WebAuthn primary** (`webauthn` / py_webauthn) + **TOTP** (`pyotp`) as the alternate second factor; **email-PIN demoted to bootstrap-only**; Google OAuth deferred. Prerequisite: verify the public hostname serves valid TLS before building 2c. | §4, §7 Stage 2, PR **2c** |
| 2 | The stored LinkedIn password | **2A — Encrypt it, and make cookie-only (`li_at`) the default; deprecate the password prompt.** Users who only supplied a password are prompted once to paste a cookie instead. | §5.2, §5.4, PR **2a** |
| 3 | Phase 2 rollout | **3A — Three sequential, independently revertible PRs: 2a encryption-at-rest → 2b identity/session hardening → 2c passkeys + recovery codes.** | §8 |

Everything below is the research that produced those three decisions; the recommendation language is
retained as written, now with the sign-off applied.

---

## 1. Where we are today (grounded in the code)

| Concern | Current implementation | Risk |
|---|---|---|
| Identity key | `users.id INT AUTO_INCREMENT` is the real PK and every FK already points at it (`compose/local/database/migrations/V4__add_user_ids.sql`), **but the application looks users up by email** — `get_user_id(email)` (`src/cqc_lem/utilities/db.py:550`), `store_cookies(user_email, …)` (`src/cqc_lem/utilities/db.py:259`), `get_cookies(url, user_email)` (`src/cqc_lem/utilities/db.py:335`). | Email is the *de-facto* identity. Change it and the code path breaks; own it and you own the account. |
| Login | Email → 6-digit PIN emailed → verify (`src/cqc_lem/api/main.py:1539` `/auth/email/init`, `src/cqc_lem/api/main.py:1590` `/auth/email/verify`). PIN is `sha256(pin + email)` (`src/cqc_lem/utilities/email.py:28`), 10-min TTL, single-use. | **Single factor, and that factor is the inbox.** Also phishable: a proxy page can relay the PIN in real time. `sha256` of a 6-digit PIN is brute-forceable from a DB dump (10⁶ candidates × known email). |
| Auth rate limiting | **None.** No throttle on `/auth/email/init` or `/auth/email/verify`. | 10⁶ PIN space with unlimited guesses inside a 10-minute window is online-brute-forceable. |
| Session token | `secrets.token_hex(32)` stored **in plaintext** in `sessions.session_token` (`create_session`, `src/cqc_lem/utilities/db.py:2351`; the INSERT is at `:2360`), sliding 24 h idle / 30-day absolute cap (`SESSION_IDLE_HOURS` / `SESSION_ABSOLUTE_MAX_DAYS`, `src/cqc_lem/utilities/env_constants.py:172-173`). Sent to the SPA and kept in `localStorage` under `lem_session` (`src/cqc_lem/ui/src/api/client.ts:16`). | A DB dump hands over **live sessions**, not just hashes. `localStorage` is XSS-exfiltratable. No per-device rows, no "sign out everywhere", no re-auth on sensitive actions. |
| LinkedIn session cookies | `cookies.value TEXT` — **plaintext** `li_at` / `JSESSIONID` (`compose/local/database/migrations/V2__add_new_table.sql`; written by `store_cookies`, `src/cqc_lem/utilities/db.py:259`). | A DB dump = a working LinkedIn session for every user. This is the crown jewel. |
| LinkedIn OAuth tokens | `users.access_token` / `users.refresh_token VARCHAR(512)` — **plaintext** (migrations `V9`, `V11`; written at `src/cqc_lem/utilities/db.py:477`). | Same. Refresh token = durable API access. |
| LinkedIn **password** | `users.password VARCHAR(255)` — stored **reversibly in plaintext** on purpose, because Selenium types it into the login form (`update_user_linkedin_password`, `src/cqc_lem/utilities/db.py:2240`; read back by `get_user_password_pair_by_id`, `src/cqc_lem/utilities/db.py:1553`). | Worst single item in the DB. Plaintext password for an account many users reuse credentials on. |
| Recovery | None beyond "receive another PIN by email". | Lost/compromised mailbox = lost or stolen account, with nothing else in the way. |
| Audit trail | `users.last_login` only (`src/cqc_lem/utilities/db.py:2364`). | No way to see or prove "who logged in, from where, and what did they touch". |
| Blast radius | 10 modules read these secrets; all go through `src/cqc_lem/utilities/db.py`. | Good news — **one choke point** to encrypt at. |

**The one-line summary:** the most valuable asset in the system (a live LinkedIn identity) is protected
by a single factor that lives in someone else's inbox, and is stored in the clear behind it.

---

## 2. Threat model (what we are actually defending against)

Ranked by realistic likelihood for a small VPS-hosted SaaS:

| # | Threat | Likely? | Today's outcome |
|---|---|---|---|
| T1 | **DB dump leaks** — stray `mysqldump` backup, snapshot, misconfigured volume, SQL injection | High | Total loss: every user's `li_at`, OAuth tokens, LinkedIn password, and live LEM sessions |
| T2 | **User's email account compromised** (credential stuffing, reused password) | Medium-high | Attacker logs into LEM and drives the victim's LinkedIn |
| T3 | **Phishing / real-time PIN relay** | Medium | Same as T2, no mailbox breach needed |
| T4 | **XSS in the SPA** | Low-medium | `localStorage` session stolen → full account |
| T5 | **Full root compromise of the VPS** | Low | Everything, always. *No design short of user-held keys survives this — see §5.3.* |
| T6 | Malicious/curious insider with read access to MySQL | Low | Same as T1 |

Design goal: **T1–T4 must not yield a usable LinkedIn session.** T5 is explicitly out of scope and we
should say so rather than pretend otherwise.

---

## 3. Identity: decouple from email

**Recommendation: keep `users.id` as the identity key** — it is already stable, already the PK, already
FK'd everywhere. Nothing needs re-keying. Three changes make email a mere attribute:

1. **`users.public_uid CHAR(36) UNIQUE`** (UUIDv4) — the id we expose externally (URLs, support,
   analytics, logs). Internal `INT` PK stays for joins; the incrementing integer stops leaking user
   counts and stops being guessable.
2. **Email becomes a verified, changeable attribute**: `users.email_verified_at`, plus a
   `user_email_history` row per change (old, new, when, by which session). `users.email` keeps its
   UNIQUE index so it stays a valid *login hint* — it just stops being the identity.
3. **Convert the email-keyed DB functions to id-keyed** — `get_user_id(email)`
   (`src/cqc_lem/utilities/db.py:550`) becomes a login-time-only lookup, and `store_cookies`
   (`:259`) / `get_cookies` (`:335`) take `user_id` like `store_linkedin_li_at` (`:361`) and
   `get_user_password_pair_by_id` (`:1553`) already do. This is the change that actually makes an
   email change safe.

**Email change flow:** confirm to **both** addresses (old + new), 24-hour hold before the old address
loses its rights, notification-only email to the old address that cannot be suppressed, audit row, and
a step-up auth requirement (§6). History and all data stay attached to the same `users.id`.

---

## 4. Authentication method — comparison

| Option | Phishing-resistant | Cost | Self-hosted | UX | Recovery story |
|---|---|---|---|---|---|
| **A. Passkeys / WebAuthn** (PyPI package `webauthn`, from the `duo-labs/py_webauthn` project — **BSD-3-Clause**) | ✅ Yes — origin-bound, nothing to relay | Free | ✅ | Face/Touch ID, one tap; sync via iCloud/Google/1Password | Needs recovery codes + a second passkey |
| **B. Password (argon2id) + mandatory TOTP** (`argon2-cffi` + `pyotp`, both MIT) | ⚠️ No — both factors are relayable through a proxy page | Free | ✅ | Familiar but two steps + an authenticator app | Recovery codes |
| **C. Email magic link / PIN** (today) | ❌ No | Free | ✅ | Easiest | Email = the recovery, which is the problem |
| **D. Google OAuth** | ✅ Yes (if the Google account has a passkey) | Free forever | ❌ third party | One click | Google's | 

Notes on each:

- **A (passkeys)** is the only option in the list that structurally defeats T3. The credential is bound
  to the origin by the browser, so a phishing proxy cannot use it. Support in 2026 is effectively
  universal (Safari 16+, Chrome 108+, Edge, Firefox 122+, iOS 16+, Android 9+, Windows Hello), and
  synced passkeys mean "new phone" is no longer a lockout event. The `webauthn` package (`py_webauthn`)
  handles registration and assertion verification in ~60 lines of server code — this is genuinely *less*
  work than B.
  **Prerequisite:** WebAuthn requires a secure context and a stable RP ID, i.e. the public hostname must
  serve valid TLS. Verify the Cloudflare-tunnel hostname's certificate before building this (there is a
  known historical DNS/TLS gap on the tunnel hostnames).
- **B** is the safe fallback and worth shipping *alongside* A as the "I can't use a passkey" path — TOTP
  is not phishing-resistant but it does defeat T2 (mailbox compromise) completely.
- **C** should be demoted to **bootstrap-only**: it may create an account and enroll the first strong
  factor, but once a passkey or TOTP exists, an email PIN alone must not be sufficient to log in.
- **D** is free forever and technically strong, but it re-introduces a third party the owner is wary of,
  and it moves the failure mode to "Google account compromised". Offer it only as an optional
  convenience, never as the sole factor. **Not recommended for now.**

**Recommendation: A primary, B as the alternate second factor, C demoted to bootstrap, D deferred.**
**→ Approved (decision 1A).** The TLS prerequisite above is a gate on PR 2c: confirm the Cloudflare-tunnel
hostname presents a valid certificate *before* the passkey work starts, since WebAuthn silently requires a
secure context.

---

## 5. Protecting the LinkedIn session at rest — the highest-impact change

This is worth more than the auth overhaul: it is the only change that mitigates **T1 and T6**, the most
likely threats, and it requires **zero UX change**.

### 5.1 Scheme — envelope encryption with per-user derived keys (AES-256-GCM)

```
LEM_SECRET_KEY  (32 random bytes, base64, in /opt/lem/.env — never in git, never in the image, never in MySQL)
      │
      ├─ HKDF-SHA256(master, salt=b"lem:user:<user_id>", info=b"<table>.<column>")  →  per-user, per-column DEK
      │
      └─ AES-256-GCM(DEK, nonce=12 random bytes, aad=b"<table>.<column>:<user_id>:<key_version>")
```

Stored as a single self-describing string in the existing column:

```
lemv1:<key_version>:<base64url(nonce)>:<base64url(ciphertext||tag)>
```

- **`cryptography` (>=42) is already a dependency** — `AESGCM` and `HKDF` are stdlib-adjacent, no new
  package, no new service.
- **AAD binds ciphertext to its row.** Copying user A's encrypted `li_at` into user B's row fails to
  decrypt — an attacker with *write* access to MySQL still cannot graft a session across accounts.
- **Per-user derived keys** avoid key-reuse volume concerns and make targeted key destruction possible
  ("burn user X's secrets" = we can't derive their key any more).
- **`key_version`** in the blob makes rotation a background re-encrypt with both keys in env
  (`LEM_SECRET_KEY` + `LEM_SECRET_KEY_PREVIOUS`), no downtime, no schema change.

### 5.2 What gets encrypted

`cookies.value` (this is `li_at` — the crown jewel), `users.access_token`, `users.refresh_token`, and
`users.password` (the stored LinkedIn password — encrypted **and** deprecated per decision 2A, §5.4).
Columns widen to `TEXT` — ciphertext is ~1.4× plus ~60 bytes of framing.

Encrypt/decrypt lives in a new `src/cqc_lem/utilities/crypto.py` and is called **only** from
`src/cqc_lem/utilities/db.py`, so all 10 consuming modules are unchanged and cannot accidentally bypass it.

### 5.3 Where the key lives — honest blast-radius analysis

| Scenario | Protected? |
|---|---|
| Stolen SQL dump / backup file / snapshot (T1) | ✅ Ciphertext only — the key is not in the dump |
| SQL injection / read access to MySQL (T1, T6) | ✅ Same |
| Compromised MySQL **container** | ✅ The key is only in the app services' env; the `mysql` container never receives it |
| Compromised LEM **account** (T2/T3) | ✅ Secrets are never returned by any API response; §6 step-up gates writes |
| **Root on the VPS (T5)** | ❌ **No.** Root can read `/opt/lem/.env` and the app's memory. |

We should not pretend otherwise. The only design that survives T5 is deriving the key from something the
**user** holds (a passphrase, or the WebAuthn PRF extension) — but LEM's Celery workers must decrypt
`li_at` at 3 a.m. while the user is asleep, so a user-held key is **architecturally incompatible with
headless automation**. That option is therefore rejected, deliberately, and T5 is mitigated
operationally (host hardening, restricted SSH, `.env` at `0600`) rather than cryptographically.

### 5.4 Cookie-only mode — approved (decision 2A)

Encrypting `users.password` is not enough on its own: it stays a *decryptable* LinkedIn password, so the
approved decision is to **stop collecting it**. `li_at` alone drives every automation path
(`store_linkedin_li_at`, `src/cqc_lem/utilities/db.py:361`), it is revocable by the user from LinkedIn's
own "Sign out of all sessions", and it is strictly less catastrophic to lose than a password many people
reuse.

What PR **2a** therefore does with the password column:

1. Encrypt the existing values along with the other three columns (nothing is dropped — password login
   keeps working for users who already have one, so no automation breaks mid-flight).
2. Make **cookie-only the default** in the Account page: `LinkedInSessionCard.tsx`
   (`src/cqc_lem/ui/src/components/LinkedInSessionCard.tsx`, `POST /api/user/linkedin-cookie`,
   `src/cqc_lem/api/main.py:2650`) becomes the primary path, and `LinkedInLoginCard.tsx`
   (`src/cqc_lem/ui/src/pages/account/LinkedInLoginCard.tsx`, `PUT /user/linkedin-password`,
   `src/cqc_lem/api/main.py:2503`) is marked deprecated/optional behind a disclosure.
3. Prompt each user who has *only* a password (no stored `li_at`) once, at next login, to paste a cookie
   instead; once they do, their stored password is deleted rather than re-encrypted.
4. `update_user_linkedin_password` (`src/cqc_lem/utilities/db.py:2240`) stays for the deprecation window
   but is gated behind step-up auth (§6, item 5) from 2b onward.

Removing the column entirely is deliberately **not** in 2a — it waits until the prompt has drained the
remaining password-only accounts, so nobody's automation stops without warning.

---

## 6. Session & account hygiene

1. **Store only `sha256(token)`** in `sessions` — a DB dump should not contain usable sessions. Same
   change for the PIN table (and give PINs an argon2id hash + attempt counter while we're there).
2. **Move the session token out of `localStorage`** into an `httpOnly; Secure; SameSite=Lax` cookie
   with a double-submit CSRF token, so XSS (T4) can no longer exfiltrate it.
3. **Per-device sessions**: `sessions` gains `user_agent`, `ip_hash`, `last_seen_at`, `revoked_at`,
   `label`. The Account page lists active devices with a per-row revoke and a "sign out everywhere".
4. **Rotate the token** on every privilege change (login, factor enrollment, email change, recovery).
5. **Step-up authentication** — the direct answer to *"a compromised login must not trivially expose the
   LinkedIn session"*. Sessions carry `last_verified_at`; endpoints that touch LinkedIn credentials
   (`PUT /user/linkedin-password`, `POST /user/linkedin-cookie`, email change, recovery-code
   regeneration, device revocation) require a **fresh** passkey/TOTP assertion within 5 minutes. Stealing
   a session is then not enough — the attacker still needs the physical factor.
   **Caveat found while auditing:** `/api/user/linkedin-cookie` is deliberately bearer-exempt
   (`src/cqc_lem/api/main.py:143-153`) because the browser extension POSTs to it with only the LEM
   `session_token` in the body. A naive step-up gate would break the one-click extension, so 2c must
   either issue the extension a short-lived, single-purpose token minted after a step-up in the SPA, or
   scope the step-up requirement to the SPA-originated path only. Decide this in 2c, don't discover it.
6. **Rate limiting + lockout** on `/api/auth/*`: Redis-backed counters per email **and** per IP
   (Redis is already in the stack), exponential backoff, hard lock after N failures, generic error
   messages that don't confirm account existence.
7. **`auth_audit_log`** table: `user_id`, `event` (login_ok, login_fail, factor_added, factor_removed,
   email_change, recovery_used, session_revoked, secret_read), `ip_hash`, `user_agent`, `created_at`.
   Surfaced on the Account page, plus an email notification on high-signal events.
8. **Recovery codes, not email**: 10 × 10-character base32 codes, argon2id-hashed, single-use, shown
   exactly once at enrollment, regenerable (which invalidates the old set). A recovery code logs you in
   and lets you enroll a new passkey — it does **not** by itself grant step-up rights to read/change
   LinkedIn credentials without also re-enrolling a factor. Losing the mailbox therefore neither locks
   the user out nor lets an attacker in.

New deps (licenses verified against each project's `LICENSE` file): `webauthn` — the `duo-labs/py_webauthn`
project — **BSD-3-Clause**; `pyotp` — **MIT**; `argon2-cffi` — **MIT**. All permissive, free, self-hosted,
no service. `cryptography` (§5.1) is already a dependency.

---

## 7. Migration plan — zero access loss

**Stage 0 — encryption (no user-visible change).**
Timestamped migration widens the four columns to `TEXT`. Read path is **dual-mode**: a value with the
`lemv1:` prefix is decrypted; anything else is treated as legacy plaintext and **lazily re-encrypted on
next write**. A one-shot backfill task encrypts everything already at rest. Once the backfill reports
zero plaintext rows, a follow-up flips `ENCRYPTION_REQUIRED=true` and the legacy read path fails closed.
Rollback during the window is safe because both formats are readable. Per decision 2A this stage also
flips the Account page to cookie-first and starts the one-time "paste a cookie instead" prompt for
password-only accounts (§5.4).

**Stage 1 — identity & sessions.**
Backfill `public_uid` for every existing user; add `email_verified_at` (backfilled to `NOW()` for users
who have already completed a PIN login — they *have* proven mailbox control). Hash existing session
tokens in place; existing sessions keep working. Convert the email-keyed DB functions to id-keyed.

**Stage 2 — strong factors.**
Passkey/TOTP enrollment ships **opt-in first**: existing users log in exactly as they do today and are
prompted (dismissible) to add a passkey and save recovery codes. After a grace period
(`REQUIRE_STRONG_FACTOR_AFTER`, env-controlled date), email-PIN alone stops being sufficient for accounts
that have a strong factor enrolled, and enrollment becomes mandatory for the rest at next login. Nobody
is ever locked out: the email PIN remains a valid *bootstrap* to enroll a factor, it just stops being a
standalone key to the LinkedIn session.

Existing users keep the same `users.id` at every stage, so posts, logs, profiles, scheduled DMs and
analytics history all stay attached.

---

## 8. Build order for Phase 2 — approved (decision 3A)

Three sequential, independently revertible PRs, in this order:

| PR | Scope | Why this order |
|---|---|---|
| **2a** ✅ shipped (#745) | `src/cqc_lem/utilities/crypto.py`, column migration, dual-mode read, backfill task, key-rotation support, **cookie-only default + password-prompt deprecation** (§5.4). Operator runbook: [`secrets-at-rest.md`](secrets-at-rest.md) | Biggest risk reduction per line of code; no login-UX change; independently shippable |
| **2b** | `public_uid`, email-as-attribute + change flow, hashed session tokens, httpOnly cookie, per-device sessions, rate limiting, `auth_audit_log` | Hardens what exists; no new login UX to design |
| **2c** | Passkeys (`webauthn`) + TOTP (`pyotp`) + recovery codes (`argon2-cffi`) + step-up gate + enrollment UI | Largest surface, benefits from 2b's session model already being in place |

Each PR carries security-focused tests (encryption round-trip + AAD-mismatch rejection + rotation,
auth flow success/failure, session revocation, rate-limit/lockout, migration backfill idempotency) at
≥90 % patch coverage, plus a threat-model note in the PR body. New migrations use TIMESTAMP versions
(`V<YYYYMMDDHHMMSS>__name.sql`), never bare integers.

---

## 9. Summary of the approved design

- **Identity:** `users.id` stays the key; add `public_uid`; email becomes a verified, changeable attribute
  with dual-confirm changes; convert email-keyed DB functions to id-keyed.
- **Auth (1A):** passkeys/WebAuthn primary (`webauthn` / py_webauthn), TOTP (`pyotp`) as the alternate
  second factor, email-PIN demoted to bootstrap-only, Google OAuth deferred. Gated on verifying the
  public hostname's TLS certificate first.
- **At rest (2A):** AES-256-GCM envelope encryption with HKDF per-user keys and row-binding AAD, master key in
  `/opt/lem/.env` only, versioned for rotation — covering `li_at`, OAuth tokens and the LinkedIn password —
  **and** cookie-only (`li_at`) becomes the default so the stored LinkedIn password is drained, not merely
  encrypted. Honest limit: this defeats a DB leak, not host root.
- **Sessions:** hashed tokens, httpOnly cookie, per-device rows + revocation, rotation on privilege
  change, **step-up auth on every endpoint that touches LinkedIn credentials**, Redis rate limiting,
  audit log.
- **Recovery:** one-time argon2id-hashed recovery codes; the mailbox stops being a single point of failure.
- **Rollout (3A):** 2a encryption → 2b identity/sessions → 2c passkeys, each independently revertible.
- **Cost:** $0. Three permissively licensed libraries (BSD-3-Clause / MIT), no new service, no third party.
