-- Engagement stats per published post, captured over time by a Selenium scraper. Powers
-- personalized post-time recommendations (which weekday×hour actually earns the user engagement).
CREATE TABLE IF NOT EXISTS post_stats (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    user_id      INT NOT NULL,
    post_id      INT NOT NULL,
    reactions    INT NOT NULL DEFAULT 0,
    comments     INT NOT NULL DEFAULT 0,
    reposts      INT NOT NULL DEFAULT 0,
    impressions  INT NULL,
    captured_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_user_post (user_id, post_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
