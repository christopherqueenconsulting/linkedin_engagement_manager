-- Appreciation-DM dedup ledger (issue #968).
--
-- Two of the three appreciation triggers (recommendation_received, collaboration) read a STANDING
-- LinkedIn surface, not an event queue: a recommendation lives on the profile forever and a mention
-- stays in the notifications feed for weeks. `automate_appreciation_dms_for_user` re-queues itself
-- every ~60s inside its loop window, so without a durable, synchronous claim the same person would
-- be thanked on every pass. This table IS that claim: one row per (user, person, event), inserted
-- BEFORE the send task is queued.
--
-- The unique key is the whole point — a person is thanked once per event type, ever. Claiming
-- before dispatch means a send that later fails is not retried: a missed thank-you is recoverable
-- by a human, a repeated one is not.
--
-- connection_accepted is included because it flows through the same dispatcher; an invitation card
-- LinkedIn leaves rendered after the accept can otherwise re-enter the list.
CREATE TABLE IF NOT EXISTS appreciation_touches (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    user_id      INT NOT NULL,
    profile_url  VARCHAR(512) NOT NULL,
    person_name  VARCHAR(255) NULL,
    event_type   ENUM('connection_accepted','recommendation_received','collaboration') NOT NULL,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_appreciation_touch (user_id, profile_url, event_type),
    KEY idx_appreciation_user_event (user_id, event_type),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
