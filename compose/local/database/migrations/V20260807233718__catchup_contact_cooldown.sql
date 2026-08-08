-- Catch-up duplicate/frequency guard (issue #1078).
--
-- `catchup_touches` already dedupes on (user, profile_url, event_type, event_period) so the SAME
-- milestone can never be messaged twice. The reporter asked for two additional guarantees:
--   1. Absolutely no duplicate sends (a retry or worker restart must never double-send).
--   2. Don't congratulate the same contact too frequently across DIFFERENT events.
--
-- This migration adds:
--   - `last_sent_at` on catchup_touches: the real send timestamp (updated when status moves to `sent`).
--   - `min_catchup_contact_interval_days` on engagement_preferences: the per-contact cooldown across
--     all catch-up event types. NULL/0 means "no cooldown beyond the per-milestone dedup".
--   - `max_catchup_touches_per_contact_days` on engagement_preferences: a per-contact cap over the
--     fixed rolling window in db.CATCHUP_CONTACT_CAP_WINDOW_DAYS (30 days), NOT over the cooldown —
--     a cap measured over the cooldown window could never be reached, because the cooldown blocks
--     the second message long before the cap could count it. NULL/0 means no per-contact cap.
--
-- Backfill `last_sent_at` from existing `sent` rows so the new cooldown logic can see history.
--
-- Default both prefs to conservative but non-blocking values: 7 days between any two catch-up
-- messages to the same person, and at most 2 messages to the same person per rolling month. These
-- defaults are small enough that they rarely fire for typical usage, but they close the burst hole.

ALTER TABLE catchup_touches
    ADD COLUMN last_sent_at DATETIME NULL AFTER updated_at;

ALTER TABLE engagement_preferences
    ADD COLUMN min_catchup_contact_interval_days INT NOT NULL DEFAULT 7;

ALTER TABLE engagement_preferences
    ADD COLUMN max_catchup_touches_per_contact_days INT NOT NULL DEFAULT 2;

UPDATE catchup_touches
SET last_sent_at = updated_at
WHERE status = 'sent' AND last_sent_at IS NULL;

-- Durable send-claim ledger: one row per milestone identity. A retry, worker restart, or lost
-- status update can never produce a second LinkedIn send because the UNIQUE key on
-- (user_id, profile_url, event_type, event_period) resolves the race at the database.
CREATE TABLE IF NOT EXISTS catchup_send_attempts (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    touch_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    profile_url VARCHAR(512) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    event_period VARCHAR(16) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY unique_send_attempt (user_id, profile_url, event_type, event_period),
    KEY idx_touch_id (touch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
