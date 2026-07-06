-- Persist the planned SUBJECT/angle of each newsletter edition. The draft queue used to generate
-- every edition INDEPENDENTLY from the newsletter's single `topic` description, producing near
-- duplicate editions ("Harness/Harnessing/Mastering AI for LinkedIn Growth ..."). We now PLAN a
-- sequence of distinct subjects up front; storing the chosen subject per edition lets the planner
-- read the history of prior editions (published, queued, AND recently skipped) and keep every new
-- edition unique + coherent. Nullable so existing editions backfill cleanly.
ALTER TABLE newsletter_editions
    ADD COLUMN subject VARCHAR(500) NULL AFTER subtitle;
