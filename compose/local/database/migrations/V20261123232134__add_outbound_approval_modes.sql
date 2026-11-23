-- Per-user approval mode for the outbound lanes (issue #2624, phase 1).
--
-- 'pre_review'   = the lane files a draft / held row and a human releases it (the default).
-- 'auto_approve' = the lane ships without a review step.
--
-- New users get 'pre_review' on every lane. Existing rows are backfilled to what each lane does
-- TODAY so nothing changes on deploy: nurture drafts and artifact DMs are always approval-gated
-- (pre_review); the weekly group post publishes unless held (auto_approve).
ALTER TABLE engagement_preferences
    ADD COLUMN nurture_dm_mode   ENUM('pre_review','auto_approve') NOT NULL DEFAULT 'pre_review',
    ADD COLUMN artifact_dm_mode  ENUM('pre_review','auto_approve') NOT NULL DEFAULT 'pre_review',
    ADD COLUMN group_post_mode   ENUM('pre_review','auto_approve') NOT NULL DEFAULT 'pre_review';

UPDATE engagement_preferences
   SET nurture_dm_mode  = 'pre_review',
       artifact_dm_mode = 'pre_review',
       group_post_mode  = 'auto_approve';
