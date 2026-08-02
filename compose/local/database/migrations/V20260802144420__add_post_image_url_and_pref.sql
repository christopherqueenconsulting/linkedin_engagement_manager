-- Single-image text posts (image-generation overhaul).
-- posts.image_url mirrors video_url: the public /api/assets URL of the AI image a TEXT post
-- publishes with. NULL = the post ships bare — an image is an enhancement, never a required
-- asset, so nothing gates on it.
ALTER TABLE posts
    ADD COLUMN image_url VARCHAR(512) NULL;

-- Per-user opt-out for generating those images (default ON — the whole point of the feature).
ALTER TABLE engagement_preferences
    ADD COLUMN text_post_images TINYINT(1) NOT NULL DEFAULT 1;
