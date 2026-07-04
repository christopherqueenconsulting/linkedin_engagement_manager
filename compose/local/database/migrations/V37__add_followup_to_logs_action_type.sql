-- v0.19.0 added LogActionType.FOLLOWUP ('followup') for DM follow-up sequence logging, but the
-- logs.action_type ENUM was never extended to include it — so process_user_followups' inserts
-- were rejected (strict mode) or silently truncated. Add 'followup' to the ENUM.
ALTER TABLE logs
    MODIFY COLUMN action_type ENUM('comment','dm','reply','post','engaged','followup') NOT NULL;
