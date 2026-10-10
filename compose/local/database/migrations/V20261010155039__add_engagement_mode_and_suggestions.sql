-- Suggest-only engagement for trial accounts (issue #2367).
--
-- `users.engagement_mode` decides whether any browser automation may run on the account's
-- LinkedIn session. `automate` is today's behaviour; `suggest` means no Selenium lane runs for the
-- user at all, and a lane that drafts a comment or DM stores it in `engagement_suggestions` for the
-- user to copy and post by hand.
--
-- The column DEFAULT is `automate` so every EXISTING account keeps exactly the behaviour it has
-- today. New accounts do NOT rely on that default: every account-creation path in
-- platform/db/repositories/users.py writes `suggest` explicitly. The runtime reader treats any
-- value it cannot read as `suggest` (fail closed).
ALTER TABLE users
    ADD COLUMN engagement_mode ENUM('suggest', 'automate') NOT NULL DEFAULT 'automate';

-- The drafts a suggest-mode lane produced instead of sending. `dedup_key` makes a re-dispatched
-- task a no-op rather than a second copy of the same suggestion.
CREATE TABLE IF NOT EXISTS engagement_suggestions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    kind ENUM('comment', 'reply', 'dm') NOT NULL,
    source VARCHAR(64) NOT NULL,
    target_url VARCHAR(1024) NULL DEFAULT NULL,
    body TEXT NOT NULL,
    dedup_key VARCHAR(255) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_engagement_suggestions_user_dedup (user_id, dedup_key),
    KEY idx_engagement_suggestions_user_created (user_id, created_at),
    CONSTRAINT fk_engagement_suggestions_user FOREIGN KEY (user_id) REFERENCES users (id)
        ON DELETE CASCADE
);
