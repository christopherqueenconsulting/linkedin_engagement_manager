-- Issue #1135: `auto_publish_newsletters` — a per-user opt-in for shipping an UNAPPROVED edition.
--
-- Until now `auto_publish_scheduled_editions` treated `status IN ('draft','approved')` as equally
-- publishable, and 'draft' is the resting status a freshly generated edition sits in. So a public
-- LinkedIn article shipped on SILENCE unless the author actively clicked Skip — right next to the
-- same edition's cover image, which is hard-gated at 'pending_review' until approved.
--
-- Two statements, and the order is the point:
--   1. ADD COLUMN ... DEFAULT 1 backfills every EXISTING row to true, so anyone currently relying
--      on drafts auto-shipping sees zero behavior change on deploy day.
--   2. SET DEFAULT 0 changes only what a NEW row gets, so a newsletter set up after this deploy
--      requires an approval before an edition publishes.
-- Splitting them is what lets the backfill and the going-forward default disagree; a single
-- `DEFAULT 0` would have silently switched off autonomous publishing for every existing user.

ALTER TABLE newsletter_settings
    ADD COLUMN auto_publish_newsletters TINYINT(1) NOT NULL DEFAULT 1;

ALTER TABLE newsletter_settings
    ALTER COLUMN auto_publish_newsletters SET DEFAULT 0;
