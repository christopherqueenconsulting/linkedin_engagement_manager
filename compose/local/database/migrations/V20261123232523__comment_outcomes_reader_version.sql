-- Comment outcome reader v2 (#2621). The v1 reader recorded reply_count=0 / author_replied=0 on every
-- comment (replies render as siblings, the expansion selector vanished, the post author was misread),
-- so its rows are kept but must stay distinguishable from a corrected re-read.
--
-- reader_version defaults to 1, so every existing row is v1. A v2 re-read of the same comment is a
-- NEW row, which is why the at-most-once key widens from (user_id, log_id) to
-- (user_id, log_id, reader_version). The user FK is still served by idx_comment_outcomes_user_checked
-- and the log FK by its own implicit index, so dropping the old unique key orphans neither.
ALTER TABLE comment_outcomes
    ADD COLUMN reader_version TINYINT NOT NULL DEFAULT 1 AFTER skip_reason,
    ADD UNIQUE KEY uq_comment_outcomes_log_version (user_id, log_id, reader_version),
    DROP INDEX uq_comment_outcomes_log;
