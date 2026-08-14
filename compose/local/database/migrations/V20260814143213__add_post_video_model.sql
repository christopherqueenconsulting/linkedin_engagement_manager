-- Which model actually rendered a post's video (issue #1410, follow-up of #1281).
--
-- `posts.video_quality` records what was REQUESTED: `_generate_video_src` degrades premium ->
-- standard when the user has no video credits and falls back to Pexels stock when the render
-- raises, and `_store_video_asset` writes every result under `videos/runwayml/` whatever produced
-- it. So neither the quality column nor the stored URL proves which model ran, and
-- `content_quality_scores.video_model_tier` could only record the coarse `runway` / `pexels`.
--
-- This column is written at render time with the `video_models.VIDEO_MODELS` key handed to
-- `create_runway_video` (`gen4_turbo` / `gen4.5` / `veo3.1_fast` / `veo3.1` / `seedance2_fast`), or
-- `pexels` for the stock fallback. NULL means the render model was never recorded — every post that
-- shipped before this column existed, and any post whose render produced no asset at all. It is not
-- backfilled: an unrecorded model falls back to the coarse tier read off the stored URL, which is
-- the same "unscored is never zero" rule applied to a string.
ALTER TABLE posts
    ADD COLUMN video_model VARCHAR(32) NULL;
