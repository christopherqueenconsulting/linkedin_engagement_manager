-- The post text a comment candidate was scored on (issue #2647, Phase 3).
--
-- Drafting happens later than the scan that found the post, with no browser open, so the draft
-- must be written from text saved at scan time. Status stays a VARCHAR; Phase 3 adds the values
-- 'drafted', 'canceled' and 'skipped' without widening anything.
--
-- Additive: one nullable column; rows saved before this migration simply have no text and are
-- never drafted.
--
-- ROLLBACK (not run by Flyway):
--   ALTER TABLE comment_candidates DROP COLUMN post_text;
-- Revert the code that reads it first.

ALTER TABLE comment_candidates
    ADD COLUMN post_text TEXT NULL;
