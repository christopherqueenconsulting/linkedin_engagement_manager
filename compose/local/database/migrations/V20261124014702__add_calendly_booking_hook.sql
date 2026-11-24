-- Outcome tracking (issue #2658, Phase 2): the Calendly polling booking hook.
-- User 1 is on Calendly Free, where webhooks are unavailable, so an hourly beat polls the API with
-- the user's personal access token. The token is stored ENCRYPTED (AAD field
-- `calendly_credentials.access_token`); it is written and read only through platform/db.
CREATE TABLE IF NOT EXISTS calendly_credentials (
    user_id         INT NOT NULL PRIMARY KEY,
    access_token    TEXT NOT NULL,
    last_polled_at  DATETIME NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_calendly_credentials_user
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- One row per Calendly invitee already applied, so replaying the same event changes nothing.
-- prior_manual_stage is the lead's manual_stage BEFORE the booking, so a cancel restores it exactly.
CREATE TABLE IF NOT EXISTS calendly_bookings (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id             INT NOT NULL,
    invitee_uri         VARCHAR(255) NOT NULL,
    event_uri           VARCHAR(255) NOT NULL,
    lead_id             INT NOT NULL,
    status              ENUM('active','canceled') NOT NULL DEFAULT 'active',
    prior_manual_stage  VARCHAR(32) NULL,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_calendly_bookings_invitee (user_id, invitee_uri),
    KEY idx_calendly_bookings_lead (lead_id),
    FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
