-- Newsletter scheduling controls + a draft-review workflow. Editions are generated a few days
-- ahead as drafts (status='draft'), the user may edit/approve them, and they auto-publish at the
-- chosen weekday/hour (user-local) — auto-publishing untouched drafts so cadence never breaks.
ALTER TABLE newsletter_settings
    ADD COLUMN publish_day  TINYINT NOT NULL DEFAULT 1,   -- 0=Mon .. 6=Sun; default Tuesday
    ADD COLUMN publish_hour TINYINT NOT NULL DEFAULT 9;   -- user-local hour of day; default 9am

CREATE TABLE IF NOT EXISTS newsletter_editions (
    id             INT AUTO_INCREMENT PRIMARY KEY,
    user_id        INT NOT NULL,
    title          VARCHAR(255) NULL,
    subtitle       VARCHAR(255) NULL,
    body           MEDIUMTEXT NULL,
    status         ENUM('draft','approved','published','failed','skipped') NOT NULL DEFAULT 'draft',
    scheduled_for  DATETIME NOT NULL,           -- UTC; when the edition should publish
    published_url  VARCHAR(512) NULL,
    generated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at   DATETIME NULL,
    updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_user_status (user_id, status),
    KEY idx_scheduled (scheduled_for, status),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
