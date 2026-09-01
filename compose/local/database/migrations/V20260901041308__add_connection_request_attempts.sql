-- Attempt ceiling for proactive connection requests (issue #1814). Nothing in the dispatch path
-- caps how many times an 'approved' row is retried, so a permanently-unreachable target (already
-- connected, no Connect route, dialog rejects the send) is indistinguishable from a transiently
-- deferred one — and each retry costs a ~90s Chrome session on the shared se_outreach lane.
--
-- Counts ONLY a real dispatch that called invite_to_connect_now and reached LinkedIn
-- (record_connection_request_attempt) — the invite hold, the daily cap and LinkedInRateLimited all
-- defer without touching this column, so a throttled target is still retried indefinitely.
ALTER TABLE connection_requests
    ADD COLUMN attempts INT NOT NULL DEFAULT 0;
