-- Per-user session windows and timezone override for the session window gate (issue #2767, Phase 3).
--
-- session_windows   JSON list of {"start": "HH:MM", "end": "HH:MM"}. NULL = the default A and C
--                   windows. The gate validates on read, so a bad value degrades to the defaults.
-- session_timezone  IANA zone name. NULL = use users.timezone.
-- Additive and nullable: no row needs a back-fill, and SESSION_WINDOW_MODE=off/shadow never read them.
ALTER TABLE engagement_preferences
    ADD COLUMN session_windows JSON NULL DEFAULT NULL,
    ADD COLUMN session_timezone VARCHAR(64) NULL DEFAULT NULL;
