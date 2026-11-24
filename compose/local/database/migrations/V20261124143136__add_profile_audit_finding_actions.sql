-- Profile audit finding actions (issue #2753, Phase 2 of #2631). Additive only.
--
-- ONE row per user per check: what the user said about that finding.
--   'ignored' is permanent: the check stays out of every later audit's open count.
--   'done' is a claim about the audit named by audit_id; the next audit re-grades the profile, so a
--   fix that did not land reopens by itself.
-- No FK on audit_id: the audit can be pruned without losing the user's ignore choice.
CREATE TABLE IF NOT EXISTS profile_audit_finding_actions (
    user_id    INT NOT NULL,
    check_id   VARCHAR(40) NOT NULL,
    action     ENUM('done','ignored') NOT NULL,
    audit_id   BIGINT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, check_id),
    CONSTRAINT fk_profile_audit_actions_user
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
