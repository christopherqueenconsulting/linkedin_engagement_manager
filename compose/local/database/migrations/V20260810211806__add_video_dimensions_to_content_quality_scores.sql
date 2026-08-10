-- Video-specific dimensions for content-quality telemetry (issue #1281).
-- Additive nullable columns: an unscored dimension (text post, failed render, missing file)
-- stays NULL rather than reading as false/zero, matching the existing content_quality_scores rule
-- that "unscored is never zero".
ALTER TABLE content_quality_scores
    ADD COLUMN video_render_ok      TINYINT(1) NULL,
    ADD COLUMN video_model_tier     VARCHAR(16) NULL,
    ADD COLUMN video_duration_seconds INT NULL,
    ADD COLUMN video_aspect_ratio   VARCHAR(16) NULL,
    ADD COLUMN video_asset_probe    VARCHAR(16) NULL;
