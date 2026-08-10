-- Skills-change re-index window (issue #1075). Profiles re-scrape periodically; when the top-5
-- ordered skills change, LEM weaves those keywords into generated content for ~14 days. The state
-- lives in Redis, but we keep the last-recorded top-5 snapshot in the profile row so detection
-- survives profile-cache misses and the source of truth remains the persisted JSON profile.
ALTER TABLE profiles
    ADD COLUMN last_recorded_skills JSON NULL AFTER data;
