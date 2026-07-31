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

-- Bootstrap: with the column defaulting to 0, NO user is an admin, so the auto-filer would park
-- every report and nobody could reach the panel to release them — the feedback->auto-work loop
-- would go silently dead on deploy. Seed the founding account (lowest id) so the loop keeps
-- running. Additional admins: UPDATE users SET is_admin = 1 WHERE email = '...', or the
-- ADMIN_USER_EMAILS env allowlist. No-op on a fresh install with no users yet.
UPDATE users SET is_admin = 1 ORDER BY id ASC LIMIT 1;
