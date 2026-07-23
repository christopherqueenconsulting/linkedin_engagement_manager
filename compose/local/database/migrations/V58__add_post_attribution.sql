-- Content attribution for the performance→content feedback loop (#386). Today post_stats can't
-- be tied to any content attribute, so we can never learn which hooks/formats/topics win.
--
-- 1) posts gains `topic` — the subject the post was built around (the post-side twin of
--    newsletter_editions.subject); the blueprint already carries archetype/hook via V51, this
--    adds the theme. Nullable so existing/manual posts backfill cleanly.
ALTER TABLE posts
    ADD COLUMN topic VARCHAR(255) NULL AFTER hook_style;

-- 2) post_stats SNAPSHOTS the post's content attributes at capture time, so the feedback loop
--    learns which shape/topic earned engagement even if the post is later edited or its shape
--    columns change. `format` mirrors posts.post_type (text/carousel/video).
ALTER TABLE post_stats
    ADD COLUMN archetype   VARCHAR(50)  NULL AFTER impressions,
    ADD COLUMN hook_style  VARCHAR(50)  NULL AFTER archetype,
    ADD COLUMN `format`    VARCHAR(50)  NULL AFTER hook_style,
    ADD COLUMN topic       VARCHAR(255) NULL AFTER `format`,
    ADD COLUMN buyer_stage VARCHAR(32)  NULL AFTER topic;

-- 3) Backfill already-captured stat rows from their post so historical stats gain attribution.
UPDATE post_stats s
    JOIN posts p ON p.id = s.post_id
SET s.archetype   = p.archetype,
    s.hook_style  = p.hook_style,
    s.`format`    = p.post_type,
    s.topic       = p.topic,
    s.buyer_stage = p.buyer_stage;
