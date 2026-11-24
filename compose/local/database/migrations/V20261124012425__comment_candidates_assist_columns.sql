-- Draft text and the approve/post trail on a comment candidate (issue #2647, Phase 4).
--
-- The LEM Assist extension reads candidates whose status is 'approved' and reports one posted. Status
-- stays a VARCHAR ('new' -> 'approved' -> 'posted'), so nothing here widens an enum. `draft_text` is
-- what the human approved; Assist inserts exactly that and never composes its own.
--
-- Additive: three nullable columns on a table nothing else reads these from yet.
--
-- ROLLBACK (not run by Flyway):
--   ALTER TABLE comment_candidates DROP COLUMN draft_text, DROP COLUMN approved_at,
--       DROP COLUMN posted_at;
-- Revert the code that reads them first.

ALTER TABLE comment_candidates
    ADD COLUMN draft_text TEXT NULL,
    ADD COLUMN approved_at TIMESTAMP NULL,
    ADD COLUMN posted_at TIMESTAMP NULL;
