-- Human-approved proactive connection requests (issue #398): the reactive profile-viewer flow already
-- sends invites via invite_to_connect, but there is no proactive, ICP-targeted connect flow. 2026
-- LinkedIn penalizes mass automation, so this stays APPROVAL-GATED and DAILY-CAPPED — an operator (or
-- an agent via the API) adds targets to a list, approves them, and LEM sends the invite via the SAME
-- invite_to_connect primitive, honoring the rate-limit / kill-switch and a per-day cap. NO volume
-- prospecting. Status mirrors scheduled_dms:
--   pending  -> draft awaiting approval
--   approved -> approved, waiting for the scanner
--   sending  -> scanner picked it up and dispatched the send task
--   sent     -> invitation sent
--   failed   -> send failed (e.g. already connected / button not found)
--   canceled -> operator canceled before send
CREATE TABLE IF NOT EXISTS connection_requests (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    recipient_profile_url VARCHAR(512) NOT NULL,
    recipient_name VARCHAR(255) NULL,
    message TEXT NULL,
    status ENUM('pending','approved','sending','sent','failed','canceled') NOT NULL DEFAULT 'pending',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_connection_requests_status (status),
    INDEX idx_connection_requests_user (user_id, status)
);

-- Conservative per-day cap for the proactive connect flow (separate from max_dms_per_day). Default 10
-- keeps it well under LinkedIn's weekly invite ceiling; operators can lower it, and the flow is still
-- gated on manual approval on top of this cap.
ALTER TABLE engagement_preferences
    ADD COLUMN max_invites_per_day INT NOT NULL DEFAULT 10;
