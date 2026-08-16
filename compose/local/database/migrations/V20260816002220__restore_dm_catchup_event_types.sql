-- Restore the catch-up DM event types the nurture migration dropped (issue #1576).
--
-- Two migrations widen the same two ENUM columns and the higher-versioned one restates a SHORTER
-- list: V20260724211808__add_catchup_touches.sql adds the six catch-up milestones (#482), then
-- V20260725001931__add_dm_nurture.sql adds 'nurture' without them (#485). Flyway runs with
-- outOfOrder=true, so which one won on any given database is the order they MERGED, not the order
-- of their versions — this migration is the UNION of both lists, so it lands the same end state
-- whichever way round they applied.
--
-- Without it, on a database where the nurture migration applied last, _schedule_catchup_followup
-- (app/engagement/outreach.py) inserts event_type='job_change' into dm_followups and MySQL strict
-- mode raises 1265, so enqueue_followup logs and returns False and the catch-up reply check never
-- exists. Saving a catch-up DM template fails the same way, taking the whole-set PUT down with it.
--
-- Additive only: every value either column has ever declared is restated here, so no row can be
-- orphaned and no data is rewritten. tests/unit/utilities/test_dm_event_vocabulary.py asserts the
-- app's vocabulary stays a subset of this list so the next MODIFY cannot narrow it again.
ALTER TABLE dm_templates
    MODIFY event_type ENUM('connection_accepted','recommendation_received','collaboration',
                           'profile_viewer','manual','funnel','nurture','job_change','promotion',
                           'work_anniversary','birthday','education','in_the_news') NOT NULL;
ALTER TABLE dm_followups
    MODIFY event_type ENUM('connection_accepted','recommendation_received','collaboration',
                           'profile_viewer','manual','funnel','nurture','job_change','promotion',
                           'work_anniversary','birthday','education','in_the_news') NOT NULL;
