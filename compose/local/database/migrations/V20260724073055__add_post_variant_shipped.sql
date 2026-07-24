-- Lightweight outcome-tracked A/B harness (issue #396 / D2). `generate_variants.py` produces
-- media/content variants for human preview only — nothing records which one actually SHIPPED for a
-- post, so realized `post_stats` can never be attributed back to the variant that earned them.
--
-- This table pins the shipped variant per post (one row per post — re-recording overwrites). Joined
-- to the post's latest `post_stats` row it lets `select_variant_winners` pick winners over time.
CREATE TABLE IF NOT EXISTS post_variants (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    user_id       INT NOT NULL,
    post_id       INT NOT NULL,
    batch_id      VARCHAR(64)  NULL,   -- generate_variants batch the shipped variant came from
    variant_index INT          NULL,   -- its index within that batch
    variant_key   VARCHAR(255) NOT NULL,  -- stable A/B identity (e.g. image|video|ratio|seed)
    combo         JSON         NULL,   -- full combo for provenance
    shipped_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_post (post_id),
    KEY idx_user (user_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
