-- Class C connection targets (#1836): a subset of profiles render an email-verification variant of
-- the Connect dialog and refuse to accept an invite without the recipient's email. Nullable — the
-- overwhelming majority of rows never carry one; it is supplied by the caller (or a future backfill
-- integration, christopherqueenconsulting/backfill) only when it is actually known.
--
-- Storage decision, made explicitly here per docs/secrets-at-rest.md: PLAINTEXT, not sealed with
-- utilities/crypto.py's envelope. This is a third party's contact detail, not a LEM credential —
-- the closest existing precedent (connection_requests.recipient_name) is also plaintext — and
-- sealing it would make it unsearchable/un-deduplicatable, which a future dedup pass would need.
-- Exposure is bounded elsewhere instead of by encryption: GET /api/connection_requests never
-- echoes this column (only a has_recipient_email boolean), no log line ever prints it, and the app
-- clears it (recipient_email = NULL) once a row reaches a terminal status (sent/failed/canceled).
ALTER TABLE connection_requests
    ADD COLUMN recipient_email VARCHAR(255) NULL;
