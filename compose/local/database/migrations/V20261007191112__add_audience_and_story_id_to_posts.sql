-- Showcase round 5: what each generated post was written FOR and FROM, recorded at generation time.
--
-- `audience` is the reader the post picked from the user's audience mix ('primary'/'secondary');
-- the next post's pick reads it back to honour the share over a rolling ten-post window.
-- `story_id` is the story-bank entry the post was anchored to; the story cooldown reads it instead
-- of guessing the entry from the post's wording (an echo match missed a paraphrased retelling).
-- Both NULL for every existing post, which the readers treat as "not recorded" — the echo
-- detection stays the fallback for those. No foreign key: a retired or deleted entry must never
-- block a post row.
ALTER TABLE posts
    ADD COLUMN audience VARCHAR(16) NULL DEFAULT NULL,
    ADD COLUMN story_id INT NULL DEFAULT NULL;
