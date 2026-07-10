-- Per-user reply/comment follow-up configuration, to cut the LinkedIn HTTP 429 rate-limiting the
-- old 24h-per-post reply-check loop caused. `reply_check_mode` selects how we follow up on comments
-- left on the user's posts:
--   event     (default) — a forwarded LinkedIn comment-notification email hits our webhook and
--                         triggers a targeted reply sweep; near-zero Selenium/429. One golden-hour
--                         safety check per post backstops a missed forward.
--   scheduled           — a beat dispatcher runs a recent-posts reply sweep reply_sweeps_per_day
--                         times/day (floor 2, cap 12 enforced in update_engagement_preferences).
--   off                 — no reply automation.
-- reply_max_post_age_days bounds how far back a sweep looks for the user's own posts (reused by
-- get_recent_posted_post_ids). These live on the single per-user engagement_preferences row.
ALTER TABLE engagement_preferences
    ADD COLUMN reply_check_mode        VARCHAR(16) NULL DEFAULT 'event' AFTER default_video_quality,
    ADD COLUMN reply_sweeps_per_day    INT         NULL DEFAULT 2       AFTER reply_check_mode,
    ADD COLUMN reply_max_post_age_days INT         NULL DEFAULT 2       AFTER reply_sweeps_per_day;

-- Persistent per-user token embedded in the comment-notification forwarding address
-- (reply+<token>@parse.<domain>). Kept on users (identity/routing), NOT on the one-row prefs upsert,
-- so it can't be clobbered by a prefs save and gets its own unique index for the webhook reverse
-- lookup (get_user_id_by_reply_token).
ALTER TABLE users
    ADD COLUMN reply_inbound_token VARCHAR(32) NULL;
ALTER TABLE users
    ADD UNIQUE INDEX ux_users_reply_inbound_token (reply_inbound_token);
