-- LinkedIn Newsletter engine config (one row per user). Newsletters bypass the feed algorithm
-- (LinkedIn pushes a notification + email to every subscriber), so this is opt-in (enabled=0 by
-- default) since publishing is a significant, subscriber-facing action. newsletter_url is filled
-- once the newsletter exists on LinkedIn; align_with_blog pulls source material from the user's
-- configured blog/sitemap when available.
CREATE TABLE IF NOT EXISTS newsletter_settings (
    id                 INT AUTO_INCREMENT PRIMARY KEY,
    user_id            INT NOT NULL UNIQUE,
    enabled            TINYINT(1) NOT NULL DEFAULT 0,
    title              VARCHAR(255) NULL,
    topic              VARCHAR(512) NULL,
    cadence            ENUM('weekly','biweekly','monthly') NOT NULL DEFAULT 'weekly',
    align_with_blog    TINYINT(1) NOT NULL DEFAULT 1,
    newsletter_url     VARCHAR(512) NULL,
    last_published_at  DATETIME NULL,
    created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
