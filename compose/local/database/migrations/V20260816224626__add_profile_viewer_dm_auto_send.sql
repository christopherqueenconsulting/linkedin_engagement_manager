-- Approval gate for cold profile-viewer outreach (issue #1137).
--
-- `engage_with_profile_viewer` is the one outreach lane that dispatched genuinely COLD contact with
-- no per-user control: a viewer we cannot comment on got a templated DM sent straight away, and a
-- non-1st-degree viewer got a personalised connection request sent straight away. Both branches now
-- file an approval-gated row instead — `scheduled_dms` / `connection_requests`, both PENDING, both
-- reviewed on surfaces that already exist — unless the user opts back into direct dispatch here.
--
-- ONE toggle governs BOTH branches on purpose: a single visit resolves to exactly one of them (we
-- are connected or we are not), so they are the same decision seen from two sides, not two settings.
--
-- 0 = off is the honest default for a NOT NULL boolean: the safest posture, and the one that keeps
-- a user who never opens Settings from continuing to cold-DM strangers unattended. Turning it on
-- restores the pre-#1137 behaviour exactly.
ALTER TABLE engagement_preferences
    ADD COLUMN profile_viewer_dm_auto_send TINYINT(1) NOT NULL DEFAULT 0;
