-- A4 AI-assistance disclosure (issue #385). LinkedIn's 2026 Authenticity Update emphasizes
-- provenance/disclosure when AI materially generates content. Per-user OPT-IN: when enabled, a
-- subtle disclosure line is appended to generated posts. Default OFF — appending a disclosure is a
-- publishing/voice decision the user must make, and existing behaviour must be unchanged until they
-- opt in. `ai_disclosure_text` lets the user override the built-in wording; NULL/blank falls back to
-- the code default (DEFAULT_AI_ASSIST_DISCLOSURE). VARCHAR(255) matches the other short prefs and is
-- clamped app-side so an over-long value can never overflow the column and roll back the whole
-- single-row engagement_preferences upsert (the V52 tone incident). Lives on the per-user prefs row.
ALTER TABLE engagement_preferences
    ADD COLUMN ai_disclosure_enabled TINYINT(1) NULL DEFAULT 0 AFTER feed_fallback_when_empty,
    ADD COLUMN ai_disclosure_text VARCHAR(255) NULL AFTER ai_disclosure_enabled;
