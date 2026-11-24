-- Per-user approval mode, phase 2 (issue #2664): the DM follow-up ladder and appreciation DMs.
--
-- 'pre_review'   = the lane files a PENDING scheduled_dms row and a human releases it (new-user default).
-- 'auto_approve' = the lane dispatches without a review step.
--
-- Both lanes send directly TODAY, so existing rows are backfilled to 'auto_approve': nothing changes
-- for anyone on deploy. A user created afterwards gets the column DEFAULT, 'pre_review'.
ALTER TABLE engagement_preferences
    ADD COLUMN followup_step_mode    ENUM('pre_review','auto_approve') NOT NULL DEFAULT 'pre_review',
    ADD COLUMN appreciation_dm_mode  ENUM('pre_review','auto_approve') NOT NULL DEFAULT 'pre_review';

UPDATE engagement_preferences
   SET followup_step_mode   = 'auto_approve',
       appreciation_dm_mode = 'auto_approve';
