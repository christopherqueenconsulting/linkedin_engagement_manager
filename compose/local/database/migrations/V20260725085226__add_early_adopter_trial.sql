-- Early-adopter extended trial (issue #499). Early adopters trade a public review for a longer
-- trial (EARLY_ADOPTER_TRIAL_DAYS, default 60) instead of the standard FREE_TRIAL_DAYS (14).
--
-- Two tables, because the two things they hold have different lifetimes:
--   early_adopter_slots  -> the ATOMIC cohort counter. One row per cohort; a slot is claimed with a
--                           single `UPDATE ... SET used = used + 1 WHERE cohort=? AND used < ?`,
--                           whose rowcount is the claim result. The cap lives in env (not here) so
--                           the owner can retune P0/P1 without a migration.
--   early_adopter_grants -> the audit trail of who got what, UNIQUE on user_id so one user can
--                           never consume two slots even under concurrent requests.
CREATE TABLE IF NOT EXISTS early_adopter_slots (
    cohort     VARCHAR(16) NOT NULL PRIMARY KEY,
    used       INT NOT NULL DEFAULT 0,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

INSERT INTO early_adopter_slots (cohort, used) VALUES ('P0', 0), ('P1', 0)
    ON DUPLICATE KEY UPDATE cohort = cohort;

CREATE TABLE IF NOT EXISTS early_adopter_grants (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    user_id       INT NOT NULL,
    cohort        VARCHAR(16) NOT NULL,
    trial_days    INT NOT NULL,
    feedback_id   INT NULL,
    trial_ends_at DATETIME NOT NULL,
    granted_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_early_adopter_user (user_id),
    INDEX idx_early_adopter_cohort (cohort, granted_at)
);
