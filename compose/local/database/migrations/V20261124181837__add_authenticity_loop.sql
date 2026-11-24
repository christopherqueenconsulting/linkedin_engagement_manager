-- Authenticity loop (#2751, Phase 3b of #2627). Additive only.
-- scheduled_dms carries the last authenticity score and the specific issues behind it, so the
-- approve path can refuse a draft below the floor and the editor can show why.
ALTER TABLE scheduled_dms
    ADD COLUMN authenticity_score TINYINT UNSIGNED NULL AFTER message,
    ADD COLUMN authenticity_issues TEXT NULL AFTER authenticity_score;

-- One row per confirm-with-reason override: who, when, the score it overrode, and why.
CREATE TABLE IF NOT EXISTS authenticity_overrides (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    lane VARCHAR(32) NOT NULL,
    item_id BIGINT UNSIGNED NOT NULL,
    score TINYINT UNSIGNED NOT NULL,
    reason VARCHAR(500) NOT NULL,
    overridden_by VARCHAR(64) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_authenticity_overrides_item (user_id, lane, item_id)
);
