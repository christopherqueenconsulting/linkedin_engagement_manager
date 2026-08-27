-- Give the COMMENTING lane the same least-recently-tried rotation `last_post_run_at` (#858) already
-- gave the POSTING lane, closing the gap: `get_enabled_group_ids` had no ORDER BY at all, so
-- `auto_comment_in_groups` always walked a user's groups in the same fixed (row) order every day.
-- Any user whose enabled-group count outgrew what `GROUP_WALK_RESERVE_SECONDS`/soft-time-limit
-- allows in one run had the SAME tail groups skipped by "ran out of time" forever — never a one-off,
-- always the same groups, which is exactly the `RecurringWarning` escalation (#1719) was built to
-- catch.
--
-- Stamped on every group the walk REACHES this run, whether or not a comment landed there (mirrors
-- `last_post_run_at`'s "tried" semantics) — a group whose feed had nothing to comment on must still
-- move to the back of the line, or an empty feed would starve the rotation exactly like an
-- unpostable group did before #858. A group the deadline causes the walk to skip is untouched, so
-- it sorts to the FRONT next run instead of being skipped again.
ALTER TABLE user_groups
    ADD COLUMN last_comment_run_at DATETIME NULL DEFAULT NULL AFTER last_post_run_at;
