-- Issue #2702: LinkedIn API terms. API-sourced and scraped post stats are no longer merged, so
-- every snapshot records which one wrote it. Readers prefer a post's `api` row, else the scraped one.
-- Existing rows were written before the API lane could be told apart from a scrape and are labelled
-- `scrape`; they are not rewritten.
ALTER TABLE post_stats
    ADD COLUMN source VARCHAR(16) NOT NULL DEFAULT 'scrape' AFTER buyer_stage;
