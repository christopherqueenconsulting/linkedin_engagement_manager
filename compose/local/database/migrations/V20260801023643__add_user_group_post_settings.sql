-- Per-group POSTING control, separate from per-group engagement (issue #769).
--
-- V41 gave `user_groups.enabled` a single meaning for two very different actions: value-add
-- COMMENTS on other people's group posts (daily) and LEM publishing its OWN post into a group
-- (weekly). A user who is happy to be commented-for in twelve groups has no way to say "but only
-- post in these two", and the weekly post silently went to whichever enabled row the database
-- happened to return first — invisible in the SPA and never rotating.
--
-- `post_enabled` splits the two so the Groups card can show (and control) them separately, and
-- `last_posted_at` makes "which group is next" a stated, least-recently-posted rotation instead of
-- an arbitrary row order. NULL last_posted_at = never posted there, which sorts FIRST so a newly
-- joined group gets its turn before a group that already had one.
--
-- The two flags are independent from here on (commenting reads `enabled`, posting reads
-- `post_enabled`), so existing rows are BACKFILLED from `enabled` rather than taking the column
-- default: a group the user had already switched off must not start receiving posts because a new
-- column defaulted to on. New rows keep DEFAULT 1, matching `enabled`'s opted-in-by-default posture.
--
-- Weekly volume is unchanged — still ONE group post per user per week; it just rotates now, and
-- which group is next is visible in the SPA.
ALTER TABLE user_groups
    ADD COLUMN post_enabled   TINYINT(1) NOT NULL DEFAULT 1 AFTER enabled,
    ADD COLUMN last_posted_at DATETIME NULL DEFAULT NULL AFTER last_synced_at;

UPDATE user_groups SET post_enabled = enabled;
