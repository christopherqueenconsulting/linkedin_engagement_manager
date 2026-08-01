-- Separate "we tried to post here" from "we posted here" (issue #858, follow-up of #769).
--
-- #769 made the weekly group post rotate on `last_posted_at`, stamped ONLY after a post actually
-- shipped so a transient failure (dead session, flaky selectors) leaves the group next in line
-- rather than skipping its turn. That is right for a transient failure and wrong for a permanent
-- one: a group whose members cannot post (admin-only / announcement) renders no share box, so its
-- `last_posted_at` stays NULL, NULL sorts FIRST, and it is "next" every week forever — starving
-- every other post-enabled group. Group sync opts joined groups into posting by default, so one
-- such group in a user's list is enough.
--
-- `last_post_run_at` is stamped by every run that REACHED the group and found it unpostable, plus
-- every successful post; the rotation orders on it. `last_posted_at` keeps its meaning untouched —
-- the truthful record of real posts, which is what the API payload and the Groups card show. A run
-- that never reached the group (no session / profile failure) stamps neither, so transient
-- behaviour is unchanged.
--
-- Backfilled from `last_posted_at` so existing rows keep their place in the rotation instead of all
-- collapsing to NULL (which would restart every user's rotation at the alphabetically first group).
ALTER TABLE user_groups
    ADD COLUMN last_post_run_at DATETIME NULL DEFAULT NULL AFTER last_posted_at;

UPDATE user_groups SET last_post_run_at = last_posted_at;
