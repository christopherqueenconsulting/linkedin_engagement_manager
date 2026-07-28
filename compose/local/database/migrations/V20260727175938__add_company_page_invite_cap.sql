-- Per-day ceiling on COMPANY-PAGE invites (issue #732). Page invites used to fire once a month and
-- deliberately drain every remaining credit in one sitting — the most bot-like velocity pattern in
-- the product, and the one outbound lane that never consulted `max_invites_per_day` or the #626
-- human-pacing engine.
--
-- This is the lane's own cap; the EFFECTIVE ceiling is min(this, max_invites_per_day), so the
-- brand-account phase policy (which already clamps max_invites_per_day) still governs the brand
-- user without a second ceiling to keep in step.
--
-- NULL means "never chosen" and reads as the conservative code default (5/day), not zero — a NULL
-- that read as 0 would silently switch page invites off for every existing row. Lives on the single
-- per-user prefs row, written by the same one-row upsert, and clamped in
-- update_engagement_preferences (the V52 lesson: one bad value must never roll back every other
-- setting).
ALTER TABLE engagement_preferences
    ADD COLUMN max_company_page_invites_per_day INT NULL DEFAULT NULL AFTER max_invites_per_day;
