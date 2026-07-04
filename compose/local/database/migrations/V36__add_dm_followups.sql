-- Scheduled multi-touch DM follow-ups. One row per pending follow-up touch to a profile.
-- Enqueued when an initial DM is sent (if a step>=1 dm_template exists); the follow-up
-- sequencer (auto_send_due_followups) sends due rows, stops the sequence if the person has
-- replied, and enqueues the next step. status tracks lifecycle.
CREATE TABLE IF NOT EXISTS dm_followups (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    user_id       INT NOT NULL,
    profile_url   VARCHAR(512) NOT NULL,
    first_name    VARCHAR(255) NULL,
    event_type    ENUM('connection_accepted','recommendation_received',
                       'collaboration','profile_viewer','manual') NOT NULL,
    next_step     INT NOT NULL,
    due_at        DATETIME NOT NULL,
    status        ENUM('pending','sent','stopped','failed') NOT NULL DEFAULT 'pending',
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_due (status, due_at),
    KEY idx_user_profile (user_id, profile_url),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
