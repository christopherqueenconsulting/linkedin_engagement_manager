-- LinkedIn Groups the user has joined + per-group on/off for LEM engagement (comment/post).
-- All groups are ENABLED by default (enabled=1). Populated by a periodic sync that scrapes the
-- user's joined groups; the user toggles which ones LEM touches in the SPA.
CREATE TABLE IF NOT EXISTS user_groups (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    user_id         INT NOT NULL,
    group_id        VARCHAR(64) NOT NULL,   -- LinkedIn group id from /groups/<id>/
    group_name      VARCHAR(255) NULL,
    enabled         TINYINT(1) NOT NULL DEFAULT 1,
    last_synced_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_user_group (user_id, group_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
