-- Issue #2702 Phase 2: LinkedIn API terms. A follower or connection count read from the LinkedIn
-- API is stored beside the scraped one but tagged, so it can be kept apart (never sent to PostHog)
-- and preferred over the scrape. Additive; existing rows are scraped.
ALTER TABLE follower_stats
    ADD COLUMN follower_source   VARCHAR(16) NOT NULL DEFAULT 'scrape' AFTER connection_count,
    ADD COLUMN connection_source VARCHAR(16) NOT NULL DEFAULT 'scrape' AFTER follower_source;
