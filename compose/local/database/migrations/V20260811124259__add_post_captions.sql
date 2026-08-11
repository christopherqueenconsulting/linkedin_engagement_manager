-- Muted-autoplay video captions (issue #1278, decision 1A: burn the text into the MP4).
--
-- The burned card is derived from the post's own opening line, so `caption_text` is the record of
-- the caption this video was given — it can no longer be re-derived once the post content is
-- edited or regenerated. `caption_srt_url` is the /api/assets URL of the .srt sidecar that was
-- written; the sidecar exists even when the burn itself is skipped (an avatar-led video without
-- the overlay opt-in) or fails open, so the author can attach captions on LinkedIn manually.
--
-- Both NULL = this post ships uncaptioned, which is the default state: the feature is behind the
-- `video-captions-enabled` flag and nothing gates on these columns.
ALTER TABLE posts
    ADD COLUMN caption_text    TEXT         NULL,
    ADD COLUMN caption_srt_url VARCHAR(512) NULL;

-- Per-user permission to paint text over an avatar-led frame. Same contract as the other avatar
-- guardrails (issue #744): default OFF, read only through guardrails/get_avatar_preferences, and
-- an unreadable row means "leave the likeness alone".
ALTER TABLE users
    ADD COLUMN avatar_caption_overlay TINYINT(1) NOT NULL DEFAULT 0;
