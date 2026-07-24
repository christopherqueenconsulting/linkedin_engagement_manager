-- LinkedIn Catch-up automation (issue #482): the /mynetwork/catch-up/ feed is a daily stream of network
-- "moments" (new job, promotion, work anniversary, birthday, education, in the news). Those are classic
-- trigger events — the warmest reason to reconnect — but a generic "Congrats!" at volume is worse than
-- nothing, so this stays APPROVAL-GATED, ICP-SCORED, DAILY-CAPPED and DEDUPED.
--
-- One row per drafted touch. `event_period` is the dedup bucket for the milestone: annual events
-- (birthday / work anniversary) bucket by YEAR, one-off events (job change / promotion / education /
-- in the news) bucket by MONTH — so the same milestone can never be messaged twice no matter how many
-- times it re-appears in the feed. Status mirrors connection_requests:
--   pending  -> drafted, awaiting human approval
--   approved -> approved, waiting for the capped scanner
--   sending  -> scanner dispatched the send task
--   sent     -> DM sent
--   skipped  -> scored below the bar / event type disabled (kept as a dedup tombstone)
--   failed   -> send failed
--   canceled -> operator canceled before send
CREATE TABLE IF NOT EXISTS catchup_touches (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    user_id         INT NOT NULL,
    profile_url     VARCHAR(512) NOT NULL,
    person_name     VARCHAR(255) NULL,
    event_type      ENUM('job_change','promotion','work_anniversary','birthday','education','in_the_news') NOT NULL,
    event_detail    VARCHAR(512) NULL,   -- the moment text as scraped ("started a new position as ...")
    event_period    VARCHAR(16) NOT NULL,  -- dedup bucket: 'YYYY' (annual) or 'YYYY-MM' (one-off)
    score           INT NOT NULL DEFAULT 0,
    message         TEXT NULL,           -- drafted congratulations DM, awaiting approval
    status          ENUM('pending','approved','sending','sent','skipped','failed','canceled') NOT NULL DEFAULT 'pending',
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_catchup_touch (user_id, profile_url, event_type, event_period),
    KEY idx_catchup_user_status (user_id, status),
    KEY idx_catchup_status (status),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Route catch-up congratulations through the existing DM template + multi-touch follow-up machinery by
-- adding one event_type per milestone. Preserves the existing enum values and appends the new ones, so a
-- replying prospect drops straight into dm_followups (the value-first nurture toward a BD conversation).
ALTER TABLE dm_templates
    MODIFY event_type ENUM('connection_accepted','recommendation_received','collaboration',
                           'profile_viewer','manual','funnel','job_change','promotion',
                           'work_anniversary','birthday','education','in_the_news') NOT NULL;
ALTER TABLE dm_followups
    MODIFY event_type ENUM('connection_accepted','recommendation_received','collaboration',
                           'profile_viewer','manual','funnel','job_change','promotion',
                           'work_anniversary','birthday','education','in_the_news') NOT NULL;

-- Per-day cap for catch-up touches. Deliberately small (5) — these are personal congratulations, not a
-- campaign; they also count against max_dms_per_day at send time.
ALTER TABLE engagement_preferences
    ADD COLUMN max_catchup_touches_per_day INT NOT NULL DEFAULT 5;

-- Approval posture. 'pre_review' (default) holds every drafted touch for explicit human approve/edit in
-- the Catch-up review UI; 'auto_approve' queues drafts straight into the capped send drip.
ALTER TABLE engagement_preferences
    ADD COLUMN catchup_touch_mode ENUM('pre_review','auto_approve') NOT NULL DEFAULT 'pre_review';

-- Which milestone types are eligible. Defaults to the BD-relevant subset (new job + promotion) so the
-- feature does not spray "Congrats!" at birthdays out of the box. JSON list of event_type values.
ALTER TABLE engagement_preferences
    ADD COLUMN catchup_event_types JSON NULL;
