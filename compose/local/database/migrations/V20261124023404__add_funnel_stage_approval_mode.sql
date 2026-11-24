-- Per-user approval mode, phase 3 (issue #2664): the later stages of the comment-first outreach funnel.
--
-- 'pre_review'   = a stage that fires drops the target to its NEXT stage as 'pending' for a human.
-- 'auto_approve' = the next stage is approved by the mode (approved_by = 'system:funnel_stage_mode').
--
-- Later stages are always pending TODAY, so existing rows are backfilled to 'pre_review': nothing
-- changes for anyone on deploy. The first stage is not covered here; it follows connection_request_mode.
ALTER TABLE engagement_preferences
    ADD COLUMN funnel_stage_mode ENUM('pre_review','auto_approve') NOT NULL DEFAULT 'pre_review';

UPDATE engagement_preferences
   SET funnel_stage_mode = 'pre_review';
