-- Re-widen dm_templates.event_type and dm_followups.event_type to every value the code writes.
--
-- Flyway runs with outOfOrder=true, so migrations reach production in MERGE order, not version
-- order. V20260725001931 (nurture, merged 2026-07-25 00:32) added 'nurture' and V20260724211808
-- (catch-up, merged 2026-07-25 02:56) has the OLDER version but merged LAST — and because an ENUM
-- widening has to restate the whole list, its MODIFY silently dropped 'nurture' again. Production
-- has therefore been missing 'nurture' since 2026-07-25 while a fresh database (applied in version
-- order) is missing the six catch-up milestones instead: two environments, two different columns.
--
-- Every auto-nurture re-check (issue #485) has been dying on
-- `1265 (01000): Data truncated for column 'event_type' at row 1` inside enqueue_followup ever
-- since, so a lead who replied got their sequence stopped and no drafted next message (issue
-- #1566). This restates the UNION of every value ever declared for these columns, so both apply
-- orders converge on the same superset and neither value set can be lost again.
--
-- Additive and independent: it widens the column, so it is safe whenever it runs and truncates no
-- existing row. tests/unit/test_migration_enum_widening.py keeps the next widening honest.
ALTER TABLE dm_templates
    MODIFY event_type ENUM('connection_accepted','recommendation_received','collaboration',
                           'profile_viewer','manual','funnel','nurture','job_change','promotion',
                           'work_anniversary','birthday','education','in_the_news') NOT NULL;
ALTER TABLE dm_followups
    MODIFY event_type ENUM('connection_accepted','recommendation_received','collaboration',
                           'profile_viewer','manual','funnel','nurture','job_change','promotion',
                           'work_anniversary','birthday','education','in_the_news') NOT NULL;
