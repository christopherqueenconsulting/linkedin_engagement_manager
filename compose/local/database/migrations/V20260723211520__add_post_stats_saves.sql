-- Saves are a strong 2026 LinkedIn reach signal (issue #387 — B2). The scraper now captures the
-- "N saves" count from the author's post analytics view; store it alongside the other per-capture
-- engagement counts. DEFAULT 0 so existing rows and posts whose bar never exposes saves stay clean.
ALTER TABLE post_stats
    ADD COLUMN saves INT NOT NULL DEFAULT 0 AFTER impressions;
