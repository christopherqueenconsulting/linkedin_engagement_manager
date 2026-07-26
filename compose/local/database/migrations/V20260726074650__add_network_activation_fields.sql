-- Network activation (issue #623). The 2026-07-26 production audit found the whole outbound layer
-- idle: connection_requests had ONE row ever (it targeted an existing 1st-degree connection, with
-- LinkedIn badge text scraped into the name), outreach_funnel_targets and scheduled_dms had none.
-- Two columns close the two data gaps behind that.

-- The connection-degree badge LinkedIn renders next to an engager's name, captured at scrape time.
-- Without it the connection-targeting scan cannot tell a 2nd-degree prospect (worth an invite) from
-- someone we are ALREADY connected to (an invite that can only fail). VARCHAR, not ENUM: LinkedIn
-- has renamed these badges before and adding an ENUM value costs a migration (see CLAUDE.md).
-- NULL means "LinkedIn didn't render a badge" — unknown, NOT "not connected".
ALTER TABLE post_engagers
    ADD COLUMN connection_degree VARCHAR(8) NULL AFTER engager_profile_url;

-- Why a send failed, so a 'failed' row in the Connections review UI says something more useful than
-- FAILED. invite_to_connect_now already produces this text ("Already connected (1st-degree)",
-- "Failed to find more or connect button: …") — it just had nowhere to live.
ALTER TABLE connection_requests
    ADD COLUMN failure_reason VARCHAR(512) NULL AFTER reasons;
