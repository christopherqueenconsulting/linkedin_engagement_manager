-- Issue #2098: `fact_hold` — the first-person specifics that hold a generated newsletter edition for
-- the owner's approval.
--
-- `auto_publish_newsletters` ships a 'draft' edition at its slot with nobody reading it, and six of
-- seven editions in the 2026-09 audit carried invented client work ("That's a real number from a
-- real client"). The generator now grades each edition's first-person numbers against the author's
-- own material; a non-empty hold keeps the draft out of auto-publish until the owner approves it.
-- 'approved' always publishes — approving IS the release.
--
-- Additive and nullable: every existing row reads NULL (not held), so nothing already queued
-- changes on deploy day.

ALTER TABLE newsletter_editions
    ADD COLUMN fact_hold VARCHAR(500) NULL;
