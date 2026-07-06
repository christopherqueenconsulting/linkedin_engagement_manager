-- Persist each edition's SHAPE, not just its subject. V49's subject history fixed repeated TOPICS,
-- but regenerated editions still all opened with the same rhetorical template and followed the same
-- skeleton. Storing the assigned format + hook style + the actual opening line (+ the compact
-- blueprint JSON for audit/UI) lets the planner and regenerator rotate away from recently used
-- SHAPES and openers across runs — every edition unique in structure, hook, and rhythm, not just
-- subject. Nullable so existing editions backfill cleanly.
ALTER TABLE newsletter_editions
    ADD COLUMN `format` VARCHAR(50) NULL AFTER subject,
    ADD COLUMN hook_style VARCHAR(50) NULL AFTER `format`,
    ADD COLUMN opening_line VARCHAR(500) NULL AFTER hook_style,
    ADD COLUMN blueprint JSON NULL AFTER opening_line;
