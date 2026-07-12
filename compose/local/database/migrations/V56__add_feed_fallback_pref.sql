-- Feed-commenting fallback: when a user's targeting INCLUDE filters (topics/keywords/authors) match
-- nothing in the feed, comment on the best feed posts anyway (hard EXCLUDES, recency, min-reactions,
-- and the daily cap still apply). LinkedIn already curates the feed to relevant content, so "anything
-- in feed" is a reasonable floor that keeps engagement flowing instead of a silent zero. Default ON;
-- users who want strict-only targeting can turn it off. Lives on the single per-user prefs row.
ALTER TABLE engagement_preferences
    ADD COLUMN feed_fallback_when_empty TINYINT(1) NULL DEFAULT 1 AFTER reply_max_post_age_days;
