-- The weekly 5-minute report email (issue #2666, Phase 1b of #2632).
--
-- weekly_report_enabled  NULL = on, 0 = opted out (same shape as review_digest_enabled). The beat is
--                    still gated by WEEKLY_REPORT_ENABLED, so this column only narrows who gets it.
ALTER TABLE engagement_preferences
    ADD COLUMN weekly_report_enabled TINYINT(1) NULL DEFAULT NULL;

-- The send claim: ONE row per user per ISO week, so a re-run or a second worker never sends twice.
-- iso_year_week is e.g. '2026-W48'. The row is INSERTed BEFORE the send and deleted if the send fails.
CREATE TABLE IF NOT EXISTS weekly_report_sends (
    id             BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id        INT NOT NULL,
    iso_year_week  VARCHAR(8) NOT NULL,
    created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_weekly_report_sends_user_week (user_id, iso_year_week),
    CONSTRAINT fk_weekly_report_sends_user
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
