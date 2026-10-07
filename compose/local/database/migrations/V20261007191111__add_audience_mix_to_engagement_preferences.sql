-- Dual-audience alternation (showcase round 5): the per-user audience mix.
--
-- A JSON object {primary_audience, secondary_audience, secondary_share, secondary_focus_topics}.
-- NULL (every existing row) and a share of 0 both mean single-audience, so no post changes until
-- the user saves a share. Shape + validation: src/cqc_lem/utilities/ai/audience_mix.py;
-- docs/content-core.md "Dual audience".
ALTER TABLE engagement_preferences
    ADD COLUMN audience_mix JSON NULL DEFAULT NULL;
