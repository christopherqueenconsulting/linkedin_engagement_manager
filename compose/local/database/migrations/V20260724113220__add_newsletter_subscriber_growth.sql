-- Newsletter subscriber-growth tracking (issue #400). Two parts:
--   1. A time-series of subscriber counts so growth can be charted over time. Each tracking run
--      records one row: the scraped subscriber_count (NULL when the page couldn't be read) plus
--      how many connections were invited that run (invites_sent) — so the same row documents both
--      the snapshot and the growth action taken.
--   2. Opt-in "invite my connections to subscribe" controls on newsletter_settings. Inviting is a
--      subscriber-facing outbound action, so it is OFF by default (invite_connections_enabled=0)
--      and hard-capped per run (max_invites_per_run) on top of LinkedIn's own weekly invite limit.
CREATE TABLE IF NOT EXISTS newsletter_subscriber_stats (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    user_id          INT NOT NULL,
    subscriber_count INT NULL,                 -- scraped subscriber count; NULL when unreadable
    invites_sent     INT NOT NULL DEFAULT 0,   -- connections invited on this run (0 when invites off/none)
    captured_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_user_captured (user_id, captured_at),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

ALTER TABLE newsletter_settings
    ADD COLUMN invite_connections_enabled TINYINT(1) NOT NULL DEFAULT 0,
    ADD COLUMN max_invites_per_run        SMALLINT   NOT NULL DEFAULT 50,
    ADD CONSTRAINT chk_nl_max_invites CHECK (max_invites_per_run BETWEEN 0 AND 500);
