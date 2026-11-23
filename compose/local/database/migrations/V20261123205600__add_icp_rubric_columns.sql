-- ICP rubric (issue #2630, Phase 1). Additive only; every column is NULLable or DEFAULTed, so
-- existing rows and the current scan read exactly as before.
--
-- engagement_preferences.icp_definition: the user's structured ICP (segments + exclusions) as JSON.
-- engagement_preferences.icp_builder_enabled: per-user flag, OFF by default. Off keeps `icp_fit`.
-- connection_requests.*: provenance written only when the rubric scored the target.
--
-- Lands after #2623, which also alters connection_requests and its insert function.
--
-- ROLLBACK (not run by Flyway): drop the columns below. Revert the code that writes them first.

ALTER TABLE engagement_preferences
    ADD COLUMN icp_definition JSON NULL,
    ADD COLUMN icp_builder_enabled TINYINT(1) NOT NULL DEFAULT 0;

ALTER TABLE connection_requests
    ADD COLUMN segment_id VARCHAR(64) NULL,
    ADD COLUMN icp_rubric_version VARCHAR(16) NULL,
    ADD COLUMN icp_components JSON NULL,
    ADD COLUMN icp_reason VARCHAR(512) NULL;
