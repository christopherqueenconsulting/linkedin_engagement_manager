-- The daily pool of scored posts a comment could go on (issue #2635, Phase 1).
--
-- One row per (user, post). `post_key` is the SAME dedup key `commented_posts.post_key` and
-- `has_user_commented_on_post_url` use (a `feedurn://urn:li:...` key, or the content-hash fallback),
-- so a candidate is skipped by the exact ledger that already stops a second comment. The table is
-- written only while COMMENT_CANDIDATE_POOL_ENABLED is on; nothing reads it yet but the ranked reader.
--
-- Additive: a new table, no change to any existing one. VARCHAR status, not ENUM (no widening hazard).
--
-- ROLLBACK (not run by Flyway): DROP TABLE comment_candidates;
-- Safe at any time once the flag is off; the writer degrades to a logged no-op on a missing table.

CREATE TABLE IF NOT EXISTS comment_candidates (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id INT NOT NULL,
    post_key VARCHAR(255) NOT NULL,
    post_url VARCHAR(1024) NULL,
    author VARCHAR(255) NULL,
    source VARCHAR(16) NOT NULL,
    score DOUBLE NOT NULL DEFAULT 0,
    score_components JSON NULL,
    scanned_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR(16) NOT NULL DEFAULT 'new',
    PRIMARY KEY (id),
    UNIQUE KEY uq_comment_candidates_user_post (user_id, post_key),
    KEY idx_comment_candidates_user_scanned (user_id, scanned_at)
);
