-- Per-user engagement preferences: voice/tone/length for AI comments, topic/keyword/author
-- targeting (so commenting/replies aren't just whatever the feed serves), per-day caps, and a
-- default buyer stage. One row per user; list-type fields are JSON arrays of strings. Missing
-- row → code-level defaults (behaviour unchanged until a user customizes).
CREATE TABLE IF NOT EXISTS engagement_preferences (
    id                     INT AUTO_INCREMENT PRIMARY KEY,
    user_id                INT NOT NULL UNIQUE,

    -- Voice / style (feeds generate_ai_response + DM refinement)
    tone                   VARCHAR(64)  NULL,
    comment_length         ENUM('short','medium','long') NOT NULL DEFAULT 'medium',
    comment_style          VARCHAR(255) NULL,
    use_emojis             TINYINT(1)   NOT NULL DEFAULT 1,
    use_hashtags           TINYINT(1)   NOT NULL DEFAULT 0,

    -- Targeting (include/exclude). JSON arrays of strings.
    include_topics         JSON NULL,
    exclude_topics         JSON NULL,
    include_keywords       JSON NULL,
    exclude_keywords       JSON NULL,
    include_authors        JSON NULL,
    exclude_authors        JSON NULL,
    post_types             JSON NULL,
    min_reactions          INT  NULL,
    reply_to_own_comments  TINYINT(1) NOT NULL DEFAULT 1,

    -- Volume caps (per day)
    max_comments_per_day   INT NOT NULL DEFAULT 20,
    max_dms_per_day        INT NOT NULL DEFAULT 20,

    -- Default buyer stage for generation when a post/context has none
    default_buyer_stage    VARCHAR(32) NULL,

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
