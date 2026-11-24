-- The scope string LinkedIn actually granted on the CMA analytics connection, so later code can check
-- scopes (r_member_profileAnalytics, r_1st_connections_size) without guessing. NULL = a connection
-- made before this column existed, whose scopes are unknown.
ALTER TABLE linkedin_member_analytics_tokens
    ADD COLUMN granted_scope VARCHAR(512) NULL AFTER access_token_created_at;
