-- Publishing cadence for the 30-day content plan (issue #621 / G6). The planner used to put one
-- post on EVERY remaining day of the month; van der Blom's 2026 sample (1.3M posts) measures daily
-- posting at roughly -26% average reach per post, and the creators who compound run 2-4 high-effort
-- posts a week on a fixed day-type calendar. `posts_per_week` is how many weekly slots that
-- calendar fills (clamped 2-7 in update_engagement_preferences; 7 = daily, kept reachable but
-- off-default and warned about in the SPA). Default 3 = Tue build-receipt / Wed story / Thu spiky
-- POV. Lives on the single per-user prefs row, so it is written by the same one-row upsert.
-- Existing planned posts are untouched — the new cadence applies to newly generated plans.
ALTER TABLE engagement_preferences
    ADD COLUMN posts_per_week INT NULL DEFAULT 3 AFTER feed_fallback_when_empty;
