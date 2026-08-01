# Secrets at rest — envelope encryption (issue #745, PR 2a)

The full rationale, threat model and the rest of the Phase-2 plan live in
[`AUTH_SECURITY_DESIGN.md`](AUTH_SECURITY_DESIGN.md). This file is the **operator** half: what to
set, how to rotate, and how to turn the fail-closed switch on without locking anyone out.

## What is protected

| Column | What it is |
|---|---|
| `cookies.value` | the LinkedIn session cookie (`li_at` / `JSESSIONID`) — the crown jewel |
| `users.access_token` | LinkedIn OAuth access token |
| `users.refresh_token` | LinkedIn OAuth refresh token (durable API access) |
| `users.password` | the stored LinkedIn password — **deprecated**, see *Cookie-only* below |

Stored as one self-describing string in the existing column:

```
lemv1:<key_version>:<base64url(nonce)>:<base64url(ciphertext||tag)>
```

AES-256-GCM under a key derived per user and per column
(`HKDF-SHA256(master, salt="lem:user:<id>", info="<table>.<column>")`), with
`"<table>.<column>:<user_id>:<key_version>"` as the AAD. That AAD is why **grafting one user's
encrypted `li_at` into another user's row fails to decrypt** — MySQL *write* access is not enough
to move a session between accounts.

`utilities/crypto.py` holds the primitives; `utilities/db.py` is the **only** caller, so the ten
modules that consume these secrets are unchanged and cannot bypass encryption.

**What this does NOT protect against:** root on the VPS. The master key is in `/opt/lem/.env`,
which root can read. That limit is deliberate and explained in the design doc §5.3 — headless
Celery workers must decrypt `li_at` at 3 a.m., so a user-held key is architecturally impossible.

## Setup

```bash
python -c "from cqc_lem.utilities.crypto import generate_master_key; print(generate_master_key())"
```

Put it in the deployment `.env` (`chmod 0600`), never in git and never in the image:

```
LEM_SECRET_KEY=<the base64 value>
LEM_SECRET_KEY_VERSION=1
```

> **Back the key up somewhere the DB backups are not.** Losing it means losing every stored
> LinkedIn session, token and password — there is no recovery path, by design.

Leaving `LEM_SECRET_KEY` empty keeps the pre-#745 behaviour (values stored as-is). That is fine for
a throwaway dev DB and is what CI runs with.

### Deploy order

The approved order (owner sign-off on PR #807) is **deploy first, key second**: this code ships with
no `LEM_SECRET_KEY` set, so it behaves exactly as it did before, then the key goes into
`/opt/lem/.env` and the next nightly `auto_encrypt_secrets_at_rest` seals every row. Nothing is
protected in the window between the two — the dual-mode read is what makes that window (and a
rollback inside it) safe, and `ENCRYPTION_REQUIRED` stays off until the backfill reports
`0 still unprotected`.

## Backfill

`auto_encrypt_secrets_at_rest` (beat: daily 03:10 UTC) rewrites every secret that is not already an
envelope under the **current** key version. It is idempotent — on an ordinary day it reads a few
rows and writes nothing — and a row it cannot decrypt is **counted and left alone**, never
overwritten, because a corrected key might still recover it.

Watch the number it logs:

```
Secret encryption backfill: 12 rewritten, 0 failed, 0 orphaned, 0 still unprotected
```

`plaintext_remaining > 0` is logged as a WARNING.

**Orphaned rows** are `cookies` rows with no `user_id`. They cannot be encrypted (there is no user
to bind the AAD to) and cannot be read back (`get_cookies` JOINs `users`) — a dead plaintext
session. They count toward `plaintext_remaining` so the fail-closed gate can never read 0 while one
exists, and the remedy is deletion, not encryption:

```sql
DELETE FROM cookies WHERE user_id IS NULL;
```

New ones can no longer be created: `_store_cookie_rows` refuses a write it cannot bind.

Run the pass on demand with:

```bash
docker exec celery_worker python -c \
  "from cqc_lem.utilities.db import encrypt_secrets_at_rest; print(encrypt_secrets_at_rest())"
```

## Rotation

No downtime, no schema change, no separate code path — rotation is the same pass as the backfill:

1. Move the current key to `LEM_SECRET_KEY_PREVIOUS` (and its version to
   `LEM_SECRET_KEY_PREVIOUS_VERSION`).
2. Put the new key in `LEM_SECRET_KEY` and bump `LEM_SECRET_KEY_VERSION`.
3. Restart the app services. Reads keep working: the `key_version` in each envelope selects the key.
4. Wait for the next nightly run (or trigger it) until it reports `0 still unprotected`.
5. Remove `LEM_SECRET_KEY_PREVIOUS`.

Skipping step 1 makes every existing row undecryptable — the key version is not guessed. Skipping
step 2's version bump is caught: if `LEM_SECRET_KEY_PREVIOUS_VERSION` ends up equal to
`LEM_SECRET_KEY_VERSION` the previous key is **ignored** and the collision logged as an ERROR,
because the alternative is worse — new writes would be sealed under the key you are about to
delete in step 5, and the backfill would report them as already current.

## Fail-closed switch

Until the backfill reports `plaintext_remaining: 0`, the read path is **dual-mode**: an envelope is
decrypted, anything else is returned as legacy plaintext. That is what makes a rollback mid-backfill
safe. Once it reaches 0:

```
ENCRYPTION_REQUIRED=true
```

Legacy plaintext then stops being readable instead of being silently accepted, and a write with no
usable key raises instead of storing plaintext.

## Cookie-only mode (design §5.4)

Encrypting `users.password` still leaves a *decryptable* LinkedIn password, so the approved end
state is to stop holding one. `li_at` alone drives every automation path, is revocable by the user
from LinkedIn's own "Sign out of all sessions", and is not a credential people reuse elsewhere.

- The session-cookie card is the default engagement login; the password form is collapsed behind a
  "not recommended" disclosure and `PUT /user/linkedin-password` is marked deprecated.
- `GET /user/account-readiness` returns `cookie_migration_needed: true` for an account whose ONLY
  engagement login is a stored password. The SPA then shows the switch prompt, and saving a cookie
  from it posts `drop_password: true`, which deletes the password (`clear_user_linkedin_password`)
  **after** the cookie is safely stored.
- The default is `false`: the browser extension posts the same body on every reconnect and must
  never silently remove a user's only working login.

Dropping the column entirely waits until the prompt has drained the remaining password-only
accounts — that is a later PR, not this one.
