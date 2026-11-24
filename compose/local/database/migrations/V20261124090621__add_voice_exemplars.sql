-- Voice exemplars, Phase 1a (issue #2627): the user's own hand-written comments and posts, embedded
-- at ingest, so a comment or DM draft can be written "like these" instead of from an adjective brief.
--
-- text_hash   sha256 of the normalised text; UNIQUE per user so a re-upload is a no-op.
-- embedding   JSON array of floats from `lem-embedding`; NULL when the embed call failed (the row
--             is kept, but never retrieved until it has a vector).
-- source      which export file the text came from.
CREATE TABLE IF NOT EXISTS voice_exemplars (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    source ENUM('comments','shares') NOT NULL,
    text_hash CHAR(64) NOT NULL,
    body TEXT NOT NULL,
    embedding JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_voice_exemplars_user_hash (user_id, text_hash),
    INDEX idx_voice_exemplars_user (user_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
