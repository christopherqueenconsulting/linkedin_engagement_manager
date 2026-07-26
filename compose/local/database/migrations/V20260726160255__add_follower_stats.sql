-- Follower & audience telemetry (issue #627). LEM tracked post stats, newsletter subscribers and
-- engagers, but never the AUDIENCE itself — follower growth and profile views are the primary
-- outcome of the whole system and were invisible.
--
-- One row per daily capture run. Every count is NULLABLE on purpose: the capture is best-effort
-- Selenium against LinkedIn's SDUI, and a missing anchor must record "not measured" rather than a
-- zero that would read as "lost all followers" in the growth deltas. profile_views and
-- search_appearances only exist on the author's own analytics surface, so they are frequently NULL.
CREATE TABLE IF NOT EXISTS follower_stats (
    id                 INT AUTO_INCREMENT PRIMARY KEY,
    user_id            INT NOT NULL,
    follower_count     INT NULL,   -- "N followers" on the user's own profile; NULL when unreadable
    connection_count   INT NULL,   -- "N connections"; NULL when unreadable ("500+" reads as 500)
    profile_views      INT NULL,   -- own-profile analytics; NULL when the surface isn't accessible
    search_appearances INT NULL,   -- own-profile analytics; NULL when the surface isn't accessible
    captured_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_user_captured (user_id, captured_at),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
