-- Admin feedback triage panel (issue #793): designate admin users and track who reviewed a feedback row.
--
--   users.is_admin        -> TINYINT(1): 1 = this user may use the feedback triage panel.
--   feedback.reviewed_by  -> user id of the admin who approved/dismissed the row.
--   feedback.reviewed_at  -> when the admin action happened.
ALTER TABLE users
    ADD COLUMN is_admin TINYINT(1) NOT NULL DEFAULT 0;

ALTER TABLE feedback
    ADD COLUMN reviewed_by INT NULL,
    ADD COLUMN reviewed_at DATETIME NULL,
    ADD INDEX idx_feedback_reviewed (reviewed_by, reviewed_at);
