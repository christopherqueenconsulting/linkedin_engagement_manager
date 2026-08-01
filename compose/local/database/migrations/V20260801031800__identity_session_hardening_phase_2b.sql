-- Issue #745 (PR 2b) — identity + session hardening.
-- Stage 2b of the approved Phase-2 build order (docs/AUTH_SECURITY_DESIGN.md §8).
-- Adds public_uid, email-change history, per-device session columns, an auth audit log and
-- PIN lockout counters, and rewrites every stored session token into its SHA-256 hash.

-- Public, non-sequential identifier — the id that may appear in URLs, logs and support tickets.
-- Backfilled for every existing row; the application fills it on user creation.
-- UNIQUE already creates the lookup index, so no separate index is added.
ALTER TABLE users
    ADD COLUMN public_uid CHAR(36) NULL UNIQUE,
    ADD COLUMN email_verified_at TIMESTAMP NULL;

UPDATE users SET public_uid = UUID() WHERE public_uid IS NULL;

-- Email is an ATTRIBUTE of the account, not its identity: this is the record of it changing.
CREATE TABLE IF NOT EXISTS user_email_history (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    old_email VARCHAR(255) NULL,
    new_email VARCHAR(255) NULL,
    changed_by_session_id INT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_user_email_history_user_id (user_id),
    CONSTRAINT fk_user_email_history_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- Per-device sessions: what the device is, when it was last used, and whether it was revoked.
ALTER TABLE sessions
    ADD COLUMN user_agent VARCHAR(512) NULL,
    ADD COLUMN ip_hash CHAR(64) NULL,
    ADD COLUMN last_seen_at TIMESTAMP NULL,
    ADD COLUMN revoked_at TIMESTAMP NULL,
    ADD COLUMN label VARCHAR(255) NULL,
    ADD INDEX idx_sessions_user_revoked (user_id, revoked_at);

-- sessions.session_token now stores SHA-256(token), never the token itself. Existing sessions keep
-- working because the application hashes the presented token before looking it up — but a DB dump
-- no longer yields a live session. Deliberately unconditional: a session token is
-- secrets.token_hex(32), which is ALSO 64 characters, so length cannot tell plaintext from hash.
-- Flyway runs a migration exactly once, so there is no second pass to hash a hash.
UPDATE sessions SET session_token = LOWER(SHA2(session_token, 256));

-- Auth audit log: login, logout, PIN failure, lockout, email change, session revoke.
-- user_id is nullable on purpose — a failed login against an unknown email has no user.
CREATE TABLE IF NOT EXISTS auth_audit_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NULL,
    email VARCHAR(255) NULL,
    event VARCHAR(50) NOT NULL,
    ip_hash CHAR(64) NULL,
    user_agent VARCHAR(512) NULL,
    session_id INT NULL,
    success TINYINT(1) NOT NULL DEFAULT 1,
    details JSON NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_auth_audit_user_event (user_id, event),
    KEY idx_auth_audit_created_at (created_at)
) ENGINE=InnoDB;

-- PIN brute-force lockout. Durable (not Redis) because it guards a 6-digit space and has to
-- survive a broker restart.
ALTER TABLE email_pin_auth
    ADD COLUMN attempts INT NOT NULL DEFAULT 0,
    ADD COLUMN locked_until TIMESTAMP NULL;
