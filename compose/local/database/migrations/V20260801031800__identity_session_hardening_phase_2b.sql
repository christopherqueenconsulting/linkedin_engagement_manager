-- Issue #745 (PR 2b) — identity + session hardening.
-- Stage 1 of the approved Phase-2 build order (docs/AUTH_SECURITY_DESIGN.md §8).
-- Adds public_uid, email verification tracking, per-device sessions, an audit log,
-- and backfills existing sessions with a hash-compatible placeholder so the auth
-- flow keeps working while tokens are rotated into SHA-256 on next use.

-- Public, non-sequential identifier exposed in URLs, logs, and support.
ALTER TABLE users
    ADD COLUMN public_uid CHAR(36) NULL UNIQUE,
    ADD COLUMN email_verified_at TIMESTAMP NULL,
    ADD INDEX idx_users_public_uid (public_uid);

-- Track which session created/confirmed an email change and when.
CREATE TABLE IF NOT EXISTS user_email_history (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    old_email VARCHAR(255) NULL,
    new_email VARCHAR(255) NULL,
    changed_by_session_id INT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_user_email_history_user_id (user_id)
) ENGINE=InnoDB;

-- Per-device sessions with revocation and a human-readable label.
-- session_token_hash holds SHA-256(session_token); existing plaintext tokens are
-- backfilled with a sentinel and rotated on the next successful login.
ALTER TABLE sessions
    ADD COLUMN user_agent VARCHAR(512) NULL,
    ADD COLUMN ip_hash CHAR(64) NULL,
    ADD COLUMN last_seen_at TIMESTAMP NULL,
    ADD COLUMN revoked_at TIMESTAMP NULL,
    ADD COLUMN label VARCHAR(255) NULL,
    ADD COLUMN device_fingerprint CHAR(64) NULL,
    ADD INDEX idx_sessions_user_revoked (user_id, revoked_at);

-- Backfill: every existing session keeps working, but its stored plaintext token
-- is replaced by SHA-256(token) so a DB dump no longer yields a live session.
-- This UPDATE is idempotent (hash of hash is not computed twice because the WHERE
-- clause filters out already-hashed rows by length).
UPDATE sessions
   SET session_token = LOWER(HEX(SHA2(session_token, 256)))
 WHERE LENGTH(session_token) <> 64;

-- Auth audit log: login, factor, email change, recovery, session revoke, secret read.
CREATE TABLE IF NOT EXISTS auth_audit_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
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

-- PIN attempts counter for lockout (per-email, per-IP optional in application).
ALTER TABLE email_pin_auth
    ADD COLUMN attempts INT NOT NULL DEFAULT 0,
    ADD COLUMN locked_until TIMESTAMP NULL;
