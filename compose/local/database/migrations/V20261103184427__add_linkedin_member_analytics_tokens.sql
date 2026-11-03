-- Temporary separate-app bridge for Community Management Development-tier analytics access.
-- Keep this token separate from users.access_token: LinkedIn binds OAuth tokens to one app, and
-- the existing LEM app remains responsible for publishing and other LinkedIn actions.
CREATE TABLE IF NOT EXISTS linkedin_member_analytics_tokens (
    user_id INT NOT NULL PRIMARY KEY,
    linked_sub_id VARCHAR(255) NOT NULL,
    access_token TEXT NOT NULL,
    access_token_expires_in INT NULL,
    access_token_created_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_linkedin_member_analytics_tokens_user
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
