-- Per-user default video quality for AUTO-generated posts. Auto-planned posts default their
-- posts.video_quality to 'standard', so they never render the account avatar's premium (Veo)
-- motion nor spend the user's video credits — even when the user HAS an active avatar + credits.
-- This preference lets a user opt their auto-generated videos up to a premium tier by default
-- (credit availability is still enforced at render time — no credits degrades to standard).
-- Lives on engagement_preferences (the single per-user content/engagement prefs row) rather than
-- a new table. VARCHAR(32) matches posts.video_quality width; values: standard | premium | premium_top.
ALTER TABLE engagement_preferences
    ADD COLUMN default_video_quality VARCHAR(32) NULL DEFAULT 'standard' AFTER default_buyer_stage;
