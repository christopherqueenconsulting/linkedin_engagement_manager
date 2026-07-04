-- Reciprocity tracking: people who commented on the user's OWN posts. The feed-commenting
-- scorer boosts these authors so we prioritize commenting back on their recent posts, seeding
-- the reciprocal engagement that grows audience. Populated by automate_reply_commenting.
CREATE TABLE IF NOT EXISTS post_engagers (
    id                   INT AUTO_INCREMENT PRIMARY KEY,
    user_id              INT NOT NULL,
    engager_name         VARCHAR(255) NOT NULL,
    engager_profile_url  VARCHAR(512) NULL,
    last_engaged_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_user_engager (user_id, engager_name),
    KEY idx_user_recent (user_id, last_engaged_at),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
