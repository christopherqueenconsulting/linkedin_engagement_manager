-- Occasion / milestone drafts published by hand (issue #1074).
-- LinkedIn's native "Celebrate an occasion" composer carries an entity the REST API cannot create,
-- so these posts are drafted by LEM and pasted into that composer by the author. The column is the
-- ONE thing that keeps the scheduler off them: `get_ready_to_post_posts` never returns a row with
-- manual_publish = 1, and `post_to_linkedin` refuses one that reaches it anyway.
-- Additive and defaulted, so every existing post stays on the automatic path.
ALTER TABLE posts
    ADD COLUMN manual_publish TINYINT(1) NOT NULL DEFAULT 0;
