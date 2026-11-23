-- Who approved a queued outbound row, so the brand-acquisition hold (#2376) can tell a human
-- Approve (the owner's send decision) from a mode-driven auto-approve (issue #2623).
--
-- Same convention as posts.approved_by (V20260924022245): 'user:<id>' for a human action,
-- 'system:<source>' for a mode-driven one, NULL when never approved or unknown. VARCHAR, not ENUM,
-- so there is no enum-widening hazard. All columns are nullable and additive: existing rows
-- read NULL, which the hold treats as "not a human approval", so current behaviour is unchanged.
-- scheduled_dms and catchup_touches are columns only here; their writers land with #2625.
--
-- ROLLBACK (not run by Flyway): drop the approved_by / approved_at columns from the four tables,
-- after reverting the code that reads them.

ALTER TABLE connection_requests
    ADD COLUMN approved_by VARCHAR(64) NULL,
    ADD COLUMN approved_at DATETIME NULL;

ALTER TABLE outreach_funnel_targets
    ADD COLUMN approved_by VARCHAR(64) NULL,
    ADD COLUMN approved_at DATETIME NULL;

ALTER TABLE scheduled_dms
    ADD COLUMN approved_by VARCHAR(64) NULL,
    ADD COLUMN approved_at DATETIME NULL;

ALTER TABLE catchup_touches
    ADD COLUMN approved_by VARCHAR(64) NULL,
    ADD COLUMN approved_at DATETIME NULL;
