-- Auto-follow-up on replies to OUR automated comments on others' posts (issue #478).
-- One row per (user, reply we've handled) so we react/reply to each reply AT MOST ONCE — dedup on
-- a stable reply key, never on text (the #474 lesson). post_key is the feedurn:// of the post we
-- commented on; reply_key is a stable id of the specific reply we handled.
CREATE TABLE IF NOT EXISTS comment_followups (
    id          INT NOT NULL AUTO_INCREMENT,
    user_id     INT NOT NULL,
    post_key    VARCHAR(255) NOT NULL,
    reply_key   VARCHAR(255) NOT NULL,
    reacted     TINYINT(1) NOT NULL DEFAULT 0,
    replied     TINYINT(1) NOT NULL DEFAULT 0,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_comment_followups_user_reply (user_id, reply_key),
    KEY idx_comment_followups_user_created (user_id, created_at),
    CONSTRAINT comment_followups_ibfk_1 FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
