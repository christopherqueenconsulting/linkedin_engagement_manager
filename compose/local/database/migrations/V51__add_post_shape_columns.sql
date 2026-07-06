-- Post SHAPE history, mirroring V50's newsletter edition shape columns: persist the assigned
-- short-form archetype + hook style for each AI-generated post so the content plan rotates a
-- user's next post AWAY from recently used shapes (same shared rotation engine the newsletter
-- uses — see content_framework.py). Nullable so existing/manual posts backfill cleanly.
ALTER TABLE posts
    ADD COLUMN archetype VARCHAR(50) NULL AFTER buyer_stage,
    ADD COLUMN hook_style VARCHAR(50) NULL AFTER archetype;
