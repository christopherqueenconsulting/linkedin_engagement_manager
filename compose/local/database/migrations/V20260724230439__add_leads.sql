-- Lead scoring & CRM-lite pipeline (issue #484): engagement signal today is scattered across
-- post_engagers, lead_signals, scheduled_dms, dm_followups, connection_requests and
-- outreach_funnel_targets, with no unified view of WHO the warm prospects are. This table is the
-- aggregate: one row per person per user, re-scored nightly by rebuild_lead_scores from data we
-- ALREADY have (no new scraping).
--
-- person_key: stable identity — 'in:<slug>' from their /in/<slug> profile URL when we have one,
--             else 'name:<lowercased name>'. The UNIQUE (user_id, person_key) makes the nightly
--             rebuild an idempotent upsert.
-- score:      0-100, ICP fit weighted by engagement recency/frequency (see utilities/lead_scoring).
-- stage:      COMPUTED warmth — cold -> warm -> hot -> in_conversation -> opportunity.
-- manual_stage / notes / dismissed: OPERATOR columns. The rebuild deliberately never writes these,
--             so a human's correction or note survives every re-score. manual_stage, when set,
--             wins over the computed stage in the UI.
CREATE TABLE IF NOT EXISTS leads (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    user_id             INT NOT NULL,
    person_key          VARCHAR(255) NOT NULL,
    person_name         VARCHAR(255) NULL,
    person_profile_url  VARCHAR(512) NULL,
    score               TINYINT UNSIGNED NOT NULL DEFAULT 0,
    icp_score           TINYINT UNSIGNED NOT NULL DEFAULT 0,
    engagement_score    TINYINT UNSIGNED NOT NULL DEFAULT 0,
    stage               ENUM('cold','warm','hot','in_conversation','opportunity') NOT NULL DEFAULT 'cold',
    signals             VARCHAR(255) NULL,   -- comma-separated signal kinds that fired
    signal_count        SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    reasons             VARCHAR(512) NULL,   -- human-readable WHY, shown on the board
    next_action         VARCHAR(255) NULL,   -- recommended next move for this stage
    first_signal_at     DATETIME NULL,
    last_signal_at      DATETIME NULL,
    manual_stage        ENUM('cold','warm','hot','in_conversation','opportunity') NULL,
    notes               VARCHAR(512) NULL,
    dismissed           TINYINT(1) NOT NULL DEFAULT 0,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_leads_user_person (user_id, person_key),
    KEY idx_leads_user_score (user_id, score),
    KEY idx_leads_user_stage (user_id, stage),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
