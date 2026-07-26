-- Target-creator engagement roster (issue #616): the curated list of accounts LEM comments on
-- FIRST, before it ever touches the home feed. The 2026 growth analysis found the feed funnel
-- examined 21 posts, matched 0 include_topics, and then fell back to commenting on off-ICP posts —
-- which under Topic Authority ranking actively damages distribution. Every fast-growing native
-- creator studied grew from high-volume substantive comments on 50-100 curated accounts blended
-- ~50% peers / 30% ICP / 20% large creators.
--
-- category                is the blend bucket the rotation draws from.
-- max_comments_per_week   is the per-author anti-pod cap (never repeatedly engage one account).
-- comments_this_week      / week_start are that cap's rolling counter, reset on a new ISO week.
-- source                  'user' = added by the operator, 'suggested' = seeded from post_engagers.
CREATE TABLE IF NOT EXISTS engagement_targets (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    profile_url VARCHAR(512) NOT NULL,
    name VARCHAR(255) NULL,
    category ENUM('peer','icp','creator') NOT NULL DEFAULT 'peer',
    max_comments_per_week TINYINT UNSIGNED NOT NULL DEFAULT 2,
    active TINYINT(1) NOT NULL DEFAULT 1,
    last_engaged_at DATETIME NULL,
    comments_this_week INT NOT NULL DEFAULT 0,
    week_start DATE NULL,
    source ENUM('user','suggested') NOT NULL DEFAULT 'user',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_engagement_targets_user_profile (user_id, profile_url),
    INDEX idx_engagement_targets_user_active (user_id, active)
);
