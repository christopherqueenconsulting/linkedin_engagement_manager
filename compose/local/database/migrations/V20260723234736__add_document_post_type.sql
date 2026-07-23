-- Native document/PDF posts (issue #390 — 2026's highest-reach format, ~6.6–7%).
-- Document posts reuse the carousel slide pipeline: slides are rendered to PNGs and
-- bundled into a single PDF at publish time, so no new asset column is needed —
-- carousel_slides carries the rendered slides for both types.
ALTER TABLE posts MODIFY COLUMN post_type ENUM('carousel', 'text', 'video', 'document') NOT NULL;
