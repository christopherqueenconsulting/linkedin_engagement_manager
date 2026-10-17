-- Suggest-only engagement for trial accounts (issue #2367).
--
-- `users.engagement_mode` decides whether any browser automation may run on the account's
-- LinkedIn session. `automate` is today's behaviour; `suggest` means no Selenium lane runs for the
-- user at all, and a lane that drafts a comment or DM stores it in `engagement_suggestions` for the
-- user to copy and post by hand.
--
-- The column DEFAULT is `automate`, so every EXISTING row reads `automate`. That is NOT what
-- decides a trial account: the runtime reader (utilities/engagement_mode.py) treats any account on
-- a trial (`subscription_status = 'trial'` or `subscription_tier = 'free_trial'`) as `suggest`
-- whatever this column says, and treats anything it cannot read as `suggest` (fail closed). New
-- accounts also get `suggest` written explicitly by every creation path in
-- platform/db/repositories/users.py.
--
-- Statement order is the replay story. MySQL DDL auto-commits, so a run that dies part-way leaves
-- what it already did. The CREATE TABLE is idempotent (IF NOT EXISTS) and goes FIRST; the ALTER is
-- the LAST statement, so either it ran (and Flyway recorded the migration) or a replay re-runs it.

-- The drafts a suggest-mode lane produced instead of sending. `dedup_key` makes a re-dispatched
-- task a no-op rather than a second copy of the same suggestion. Rows go with their user
-- (ON DELETE CASCADE).
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

ALTER TABLE users
    ADD COLUMN engagement_mode ENUM('suggest', 'automate') NOT NULL DEFAULT 'automate';
