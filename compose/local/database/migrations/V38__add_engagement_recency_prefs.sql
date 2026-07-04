-- Recency-aware feed targeting. `max_post_age_hours` gates how old a feed post may be before the
-- commenting engine skips it (golden-hour prioritization — recent posts get far more algorithmic
-- weight and the author is online to reply). Also flip the comment_length default to 'short':
-- engagement research shows short, specific, question-ending comments earn replies; long ones don't.
ALTER TABLE engagement_preferences
    ADD COLUMN max_post_age_hours INT NULL DEFAULT 24 AFTER min_reactions,
    MODIFY COLUMN comment_length ENUM('short','medium','long') NOT NULL DEFAULT 'short';
