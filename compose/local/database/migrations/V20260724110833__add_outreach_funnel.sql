-- Comment-first outreach funnel (issue #399): a sequenced, APPROVAL-GATED
-- value-comment -> connect -> DM funnel keyed to a per-user target list. The comment/connect/DM
-- send primitives (comment_on_post, invite_to_connect, send_private_dm) and the DM
-- template/follow-up machinery already exist; this table is the missing per-prospect state store.
--
-- One row per (user_id, target_profile_url). `stage` is where the prospect is in the funnel and
-- `status` is the state of that stage. A stage only fires when a human has approved it
-- (status='approved'); firing it advances the target to the NEXT stage as 'pending', which requires
-- a fresh approval — so no step ever auto-fires at volume. Status/stage mirror the scheduled_dms
-- pending->approved->acted lifecycle:
--   stage:  comment -> connect -> dm -> completed
--   status: pending  -> draft awaiting human approval
--           approved -> human approved; the funnel processor will fire this stage on its next run
--           acted    -> terminal: the final (dm) stage fired
--           skipped  -> current stage skipped without firing (no draft to act on)
--           failed   -> firing the stage errored (e.g. comment stage with no post URL)
--           canceled -> operator canceled the whole funnel for this target
CREATE TABLE IF NOT EXISTS outreach_funnel_targets (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    user_id             INT NOT NULL,
    target_profile_url  VARCHAR(512) NOT NULL,
    target_name         VARCHAR(255) NULL,
    stage               ENUM('comment','connect','dm','completed') NOT NULL DEFAULT 'comment',
    status              ENUM('pending','approved','acted','skipped','failed','canceled') NOT NULL DEFAULT 'pending',
    context_url         VARCHAR(512) NULL,   -- post to comment on (comment stage)
    draft_text          TEXT NULL,           -- drafted action text for the current stage, awaiting approval
    notes               VARCHAR(512) NULL,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_funnel_user_target (user_id, target_profile_url),
    KEY idx_funnel_user_status (user_id, status),
    KEY idx_funnel_stage_status (stage, status),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Route funnel DMs through the existing DM template + follow-up machinery by adding a 'funnel'
-- event_type. Preserves the existing enum values and appends the new one.
ALTER TABLE dm_templates
    MODIFY event_type ENUM('connection_accepted','recommendation_received','collaboration',
                           'profile_viewer','manual','funnel') NOT NULL;
ALTER TABLE dm_followups
    MODIFY event_type ENUM('connection_accepted','recommendation_received','collaboration',
                           'profile_viewer','manual','funnel') NOT NULL;
