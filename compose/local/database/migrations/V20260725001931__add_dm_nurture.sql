-- DM conversation auto-nurture (issue #485): when a lead replies to one of our DMs we stop the
-- sequence and the thread goes cold. This migration adds the two things the nurture path needs on
-- top of the machinery that already exists (dm_templates / dm_followups / scheduled_dms):
--
-- 1. A 'nurture' event_type, so the drafted next message uses the SAME operator-editable template +
--    follow-up sequencing as every other DM touch (mirrors how issue #399 added 'funnel'). Step 0
--    is the first message after their reply; higher steps are the later touches in that thread.
-- 2. scheduled_dms.source, marking rows an automation drafted. The nurture path is approval-gated —
--    the draft lands as 'pending' in the SAME queue the operator already reviews — and this column
--    is what lets it dedup one open draft per thread and enforce its own per-day cap without
--    counting the DMs an operator wrote by hand. NULL = operator-created (existing rows).
ALTER TABLE dm_templates
    MODIFY event_type ENUM('connection_accepted','recommendation_received','collaboration',
                           'profile_viewer','manual','funnel','nurture') NOT NULL;
ALTER TABLE dm_followups
    MODIFY event_type ENUM('connection_accepted','recommendation_received','collaboration',
                           'profile_viewer','manual','funnel','nurture') NOT NULL;

-- The dedup/cap lookups are always keyed (user, source, status).
ALTER TABLE scheduled_dms
    ADD COLUMN source VARCHAR(32) NULL AFTER message,
    ADD INDEX idx_scheduled_dms_source (user_id, source, status);
