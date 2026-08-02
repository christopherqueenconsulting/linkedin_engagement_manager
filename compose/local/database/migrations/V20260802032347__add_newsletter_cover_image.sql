-- Issue #893: newsletter cover images — user upload AND opt-in AI generation.
--
-- A cover is a PUBLIC brand asset, so a generated one is never attached straight to a publish:
-- `cover_image_status` is 'pending_review' until the author approves it, and the publish flow
-- attaches only an 'approved' cover. An uploaded cover is the author's own artwork, so it lands
-- 'approved' on upload.
--
-- `cover_image_path` is stored RELATIVE to the app's assets dir (e.g.
-- 'images/newsletter_covers/5/ed12_ab12cd.png') so it maps straight onto /api/assets?file_name=
-- and survives a container path change.

ALTER TABLE newsletter_editions
    ADD COLUMN cover_image_path   VARCHAR(512) NULL AFTER blueprint,
    ADD COLUMN cover_image_source ENUM('upload','ai') NULL AFTER cover_image_path,
    ADD COLUMN cover_image_status ENUM('pending_review','approved') NULL AFTER cover_image_source;

-- Account-settings opt-in: generation costs money per edition, so it is OFF unless the author
-- turns it on for their newsletter.
ALTER TABLE newsletter_settings
    ADD COLUMN cover_image_auto TINYINT(1) NOT NULL DEFAULT 0;
