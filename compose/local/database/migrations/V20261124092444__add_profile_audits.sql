-- Profile audit history (issue #2631, Phase 1). Additive only.
--
-- profile_audits: ONE row per user per kind per period. period_key is '2026-W48' (weekly) or
-- '2026-11' (monthly); the UNIQUE key is what makes a beat re-run a no-op.
-- score is NULL when no check was readable. closed_count / opened_count are the delta against the
-- previous audit, so a fix shows up as a closed finding.
CREATE TABLE IF NOT EXISTS profile_audits (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id          INT NOT NULL,
    kind             ENUM('weekly','monthly') NOT NULL,
    period_key       VARCHAR(10) NOT NULL,
    rubric_version   VARCHAR(16) NOT NULL,
    score            SMALLINT NULL,
    previous_audit_id BIGINT NULL,
    closed_count     SMALLINT NOT NULL DEFAULT 0,
    opened_count     SMALLINT NOT NULL DEFAULT 0,
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_profile_audits_user_kind_period (user_id, kind, period_key),
    KEY idx_profile_audits_user_created (user_id, created_at),
    CONSTRAINT fk_profile_audits_user
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS profile_audit_findings (
    audit_id   BIGINT NOT NULL,
    check_id   VARCHAR(40) NOT NULL,
    status     ENUM('pass','warn','fail','unknown') NOT NULL,
    weight     SMALLINT NOT NULL,
    detail     VARCHAR(255) NOT NULL DEFAULT '',
    fix_hint   VARCHAR(255) NOT NULL DEFAULT '',
    PRIMARY KEY (audit_id, check_id),
    CONSTRAINT fk_profile_audit_findings_audit
        FOREIGN KEY (audit_id) REFERENCES profile_audits(id) ON DELETE CASCADE
);
