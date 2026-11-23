-- Opt-out for the daily stale-approval email (issue #2625, Phase 1).
--
-- review_digest_enabled  NULL = on. The digest is a reminder the author never asked for by name, so
--                    "never chose" and "left on" must read identically, and a NULL column lets the
--                    migration add it without back-filling every existing row. 0 is the only value
--                    that silences it.
ALTER TABLE engagement_preferences
    ADD COLUMN review_digest_enabled TINYINT(1) NULL DEFAULT NULL;
