-- Link-in-first-comment mechanic (issue #392 - C3). An external link in the post BODY costs
-- ~60-68% reach on LinkedIn; the same link in the author's FIRST COMMENT costs nothing. At publish
-- time the body is split: carried links are stripped from what publishes and stashed here, then the
-- seed-comment task (auto_seed_comment_on_post) appends them to the first comment it leaves.
-- Newline-separated when a post carries more than one link. NULL = nothing to carry.
ALTER TABLE posts ADD COLUMN first_comment_link VARCHAR(1024) NULL;

-- Opt-out for the mechanic. Default ON (it is the 2026 best practice); users who want their links
-- inline can turn it off. Lives on the single per-user prefs row like every other engagement pref.
ALTER TABLE engagement_preferences
    ADD COLUMN link_in_first_comment TINYINT(1) NULL DEFAULT 1 AFTER feed_fallback_when_empty;
