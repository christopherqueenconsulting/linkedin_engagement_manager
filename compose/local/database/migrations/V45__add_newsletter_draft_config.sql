-- Per-user controls for planning newsletter editions ahead. Instead of a single draft generated a
-- fixed few days out, users choose how far ahead drafts appear and how many to keep queued so they
-- can review/edit without feeling rushed.
--   generate_lead_days: days before its slot the first draft is generated (was hardcoded 3).
--   max_queued_drafts:  how many unpublished draft/approved editions to keep queued (1-10).
-- Defaults (3, 1) backfill existing rows, preserving today's single-draft behavior until a user opts in.
ALTER TABLE newsletter_settings
    ADD COLUMN generate_lead_days TINYINT NOT NULL DEFAULT 3,
    ADD COLUMN max_queued_drafts  TINYINT NOT NULL DEFAULT 1,
    ADD CONSTRAINT chk_nl_lead_days  CHECK (generate_lead_days BETWEEN 0 AND 60),
    ADD CONSTRAINT chk_nl_max_queued CHECK (max_queued_drafts  BETWEEN 1 AND 10);

-- Idempotency backstop: never two editions for the same user+slot, so a repeated generation run
-- (or a rare double beat) can't create duplicate drafts for the same scheduled time.
ALTER TABLE newsletter_editions
    ADD UNIQUE KEY uq_user_slot (user_id, scheduled_for);
