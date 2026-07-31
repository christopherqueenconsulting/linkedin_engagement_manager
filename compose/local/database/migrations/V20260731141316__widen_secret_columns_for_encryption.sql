-- Issue #745 (PR 2a) — widen the columns that now hold AES-256-GCM envelopes.
-- `lemv1:<version>:<b64url nonce>:<b64url ciphertext||tag>` is ~1.4x the plaintext plus ~60 bytes
-- of framing, so a 512-char OAuth token no longer fits VARCHAR(512) and a long LinkedIn password
-- no longer fits VARCHAR(255). Widening only — no data is read, rewritten or dropped here; the
-- backfill task (auto_encrypt_secrets_at_rest) encrypts what is already at rest, and the read path
-- stays dual-mode so a rollback during the window is safe.
--
-- cookies.value is already TEXT (V2), so it needs no change.

ALTER TABLE users MODIFY COLUMN password TEXT NULL;

ALTER TABLE users MODIFY COLUMN access_token TEXT NULL;

ALTER TABLE users MODIFY COLUMN refresh_token TEXT NULL;
