-- Class C connection targets (#1836): a subset of profiles render an email-verification variant of
-- the Connect dialog and refuse to accept an invite without the recipient's email. Nullable, and
-- NULL for every existing row — this migration adds the column and nothing else.
--
-- No backfill, and no derivation from contact data LEM already holds (owner decision, 2026-09-01).
-- A value exists here only because a human typed it against one specific row: putting an address
-- nobody chose per row into a dialog on a third-party site is exactly what was rejected. Anything
-- that populates this column in bulk is a new decision, not an extension of this one.
--
-- Storage decision, made explicitly here per docs/secrets-at-rest.md: PLAINTEXT, not sealed with
-- utilities/crypto.py's envelope. This is a third party's contact detail, not a LEM credential —
-- the closest existing precedent (connection_requests.recipient_name) is also plaintext.
-- Exposure is bounded elsewhere instead of by encryption: GET /api/connection_requests never
-- echoes this column (only a has_recipient_email boolean), no log line ever prints it, and the app
-- clears it (recipient_email = NULL) once a row reaches a terminal status (sent/failed/canceled).
ALTER TABLE connection_requests
    ADD COLUMN recipient_email VARCHAR(255) NULL;
