-- Smart connection targeting from content engagers (issue #486, extends #398). #398 gave us the
-- approval-gated, daily-capped SEND path (connection_requests + invite_to_connect); this adds the
-- SOURCING half: people who engaged with the user's own posts, and people who engaged with a
-- configurable set of adjacent authors' recent posts, ICP-scored and deduped before they ever
-- become a request. Nothing here bypasses #398's gates — every target still lands in
-- connection_requests and still costs one slot of max_invites_per_day.

-- Provenance on the request itself, so the operator approving in the Connections review UI can see
-- WHERE this person came from and WHY they scored well. Nullable: rows added by hand (or by #398's
-- API) carry no source and must keep working unchanged.
ALTER TABLE connection_requests
    ADD COLUMN source    VARCHAR(32)  NULL AFTER message,
    ADD COLUMN icp_score TINYINT UNSIGNED NULL AFTER source,
    ADD COLUMN reasons   VARCHAR(512) NULL AFTER icp_score;

-- Targeting configuration.
--   connection_targeting_mode — 'off' disables sourcing entirely; 'suggest' (default) ALWAYS files
--     candidates as 'pending' drafts needing a human approve, regardless of connection_request_mode;
--     'auto_queue' defers to connection_request_mode (so auto_approve users get the capped drip).
--     Default 'suggest' means turning this on can never send outbound on its own.
--   connection_target_authors — adjacent thought-leaders'/competitors' profile URLs whose recent
--     posts we harvest engagers from (warm 2nd-degree prospects). Empty = own posts only.
--   min_connection_icp_score — ICP floor for people we HAVE profile facts on. People we've never
--     scraped score neutral (lead_scoring.ICP_UNKNOWN) and are judged on engagement instead, since
--     most engagers are never scraped and a floor would otherwise reject everyone.
-- VARCHAR (not ENUM) for the mode: adding an ENUM value needs a migration (see CLAUDE.md), and this
-- list will grow.
ALTER TABLE engagement_preferences
    ADD COLUMN connection_targeting_mode VARCHAR(16)      NOT NULL DEFAULT 'suggest',
    ADD COLUMN connection_target_authors JSON             NULL,
    ADD COLUMN min_connection_icp_score  TINYINT UNSIGNED NOT NULL DEFAULT 55;
