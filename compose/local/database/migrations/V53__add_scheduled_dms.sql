-- Scheduled 1:1 DMs (issue #306): mirror the post scheduler for direct messages so an operator (or
-- an agent via the API) can draft a DM to a named prospect, approve it, and have LEM send it at a
-- chosen time. The send primitive (send_private_dm, routed to se_outreach) and templates already
-- exist; this table is the missing scheduling store. Status mirrors posts:
--   pending  -> draft awaiting approval
--   approved -> approved, waiting for its scheduled_time
--   scheduled-> scanner picked it up and dispatched the send task
--   sent     -> delivered
--   failed   -> send failed (e.g. not a 1st-degree connection)
--   canceled -> operator canceled before send
CREATE TABLE IF NOT EXISTS scheduled_dms (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    recipient_profile_url VARCHAR(512) NOT NULL,
    recipient_name VARCHAR(255) NULL,
    message TEXT NOT NULL,
    scheduled_time DATETIME NOT NULL,
    status ENUM('pending','approved','scheduled','sent','failed','canceled') NOT NULL DEFAULT 'pending',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_scheduled_dms_due (status, scheduled_time),
    INDEX idx_scheduled_dms_user (user_id, status)
);
