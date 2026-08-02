-- Issue #745 (PR 2c) — passkeys, TOTP, recovery codes and the step-up gate.
-- Stage 2c of the approved Phase-2 build order (docs/AUTH_SECURITY_DESIGN.md §8).
-- Nothing here is destructive and nothing is backfilled: every account starts with zero strong
-- factors and keeps logging in exactly as it does today until it enrols one (design §7 Stage 2).

-- The strong factors an account holds. ONE table with a `kind` discriminator rather than a
-- passkey table and a TOTP table, because every read is "does this user have a strong factor"
-- and every write is enrol/remove — splitting it would mean two queries for one question.
--
-- `credential_id_hash` (not `credential_id`) carries the UNIQUE index: a WebAuthn credential id is
-- up to 1023 raw bytes, which base64url-encodes past what MySQL will index, and a truncated-prefix
-- unique key would silently reject a legitimate authenticator that shares a prefix.
--
-- `secret` holds the TOTP shared secret as a `lemv1:` envelope (utilities/crypto.py, PR 2a) bound
-- to this user — a DB dump yields no working authenticator seed. It is NULL for passkeys, whose
-- public key is by definition public.
--
-- `sign_count` is the factor's monotonic counter for BOTH kinds: a passkey's WebAuthn signature
-- count, and an authenticator app's last accepted TOTP time step. Each must strictly increase,
-- which is what makes a cloned authenticator and a re-typed 30-second code fail rather than pass.
CREATE TABLE IF NOT EXISTS user_auth_factors (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    kind VARCHAR(16) NOT NULL,
    label VARCHAR(120) NULL,
    credential_id TEXT NULL,
    credential_id_hash CHAR(64) NULL,
    public_key TEXT NULL,
    sign_count BIGINT NOT NULL DEFAULT 0,
    transports VARCHAR(255) NULL,
    secret TEXT NULL,
    confirmed_at TIMESTAMP NULL,
    last_used_at TIMESTAMP NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_auth_factor_credential (credential_id_hash),
    KEY idx_auth_factors_user_kind (user_id, kind),
    CONSTRAINT fk_user_auth_factors_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- Single-use recovery codes, argon2id-hashed (design §6.8). The row is kept after use so the
-- account page can say "3 of 10 remaining" and the audit trail can show one being spent.
CREATE TABLE IF NOT EXISTS user_recovery_codes (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    code_hash VARCHAR(255) NOT NULL,
    used_at TIMESTAMP NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_recovery_codes_user (user_id, used_at),
    CONSTRAINT fk_user_recovery_codes_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- Short-lived server state for a ceremony in flight: the WebAuthn challenge the authenticator must
-- sign, and the pending-login handle issued when a PIN succeeded but a second factor is still owed.
--
-- Deliberately MySQL and not Redis. Redis holds runtime state that is allowed to fail open (the
-- pacing seeds, the auth rate limiter); a challenge store that fails open would let a replayed
-- assertion through, so this one has to be durable and fail closed.
--
-- `user_id` is nullable: a passkey login with a discoverable credential does not know who is
-- signing in until the assertion comes back.
--
-- `attempts` is the DURABLE guessing bound on a pending second-factor login. The Redis auth limiter
-- in front of it fails open by design, so on its own it is not what stops a 6-digit code being
-- walked; burning the handle after a fixed number of wrong codes is. It is a counter and not a
-- consume-on-first-touch because a login that dies on one mistyped digit is a dead end, not a
-- control — the user has no way back except starting the whole email round trip again.
CREATE TABLE IF NOT EXISTS auth_challenges (
    id INT AUTO_INCREMENT PRIMARY KEY,
    handle_hash CHAR(64) NOT NULL,
    user_id INT NULL,
    purpose VARCHAR(32) NOT NULL,
    challenge VARCHAR(255) NULL,
    attempts INT NOT NULL DEFAULT 0,
    expires_at TIMESTAMP NOT NULL,
    consumed_at TIMESTAMP NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_auth_challenges_handle (handle_hash),
    KEY idx_auth_challenges_expires (expires_at)
) ENGINE=InnoDB;

-- Step-up authentication (design §6.5). `last_verified_at` is when this SESSION last proved a
-- strong factor; the credential-touching endpoints require it to be inside STEP_UP_MAX_AGE_MINUTES.
--
-- `scope` separates the browser-extension session from a full one. The extension POSTs li_at with
-- only its own session token and can never run a passkey ceremony, so its step-up happens ONCE, in
-- the SPA, at the moment the token is minted — after which the token itself carries the right to
-- store a cookie and nothing else.
--
-- The third value, `recovery`, marks a session that got in with a recovery code. It is what keeps
-- "you may not enrol an additional factor without proving one" from becoming a lockout for the one
-- person who legitimately cannot: someone who lost the factor they had.
ALTER TABLE sessions
    ADD COLUMN last_verified_at TIMESTAMP NULL,
    ADD COLUMN scope VARCHAR(32) NOT NULL DEFAULT 'full';
