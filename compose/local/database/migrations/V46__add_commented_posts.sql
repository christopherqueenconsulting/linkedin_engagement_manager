-- At-most-once feed commenting: a persistent, cross-run/worker claim ledger keyed by a stable
-- per-post key. SDUI feed posts have no reliable permalink, so post_key is either the real
-- /feed/update/ URL when present, else a synthetic content+author hash (_feed_post_key). The
-- UNIQUE (user_id, post_key) makes the claim atomic: the pre-post run, the golden-hour run, a
-- retried run, and any concurrent worker all race on the same INSERT and the losers back off,
-- so a post can be commented on at most once. status: 'claimed' (in-flight) -> 'commented'
-- (posted); a claim that never posts is released (row deleted) so a later run can retry.
CREATE TABLE IF NOT EXISTS commented_posts (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    user_id     INT NOT NULL,
    post_key    VARCHAR(255) NOT NULL,
    status      VARCHAR(20) NOT NULL DEFAULT 'claimed',
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_commented_posts_user_key (user_id, post_key),
    KEY idx_commented_posts_user (user_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
