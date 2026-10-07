-- Per-user brand kit image generation reads (palette, typography feel, mood, motifs to avoid).
-- NULL = no kit: the image engine keeps its neutral grading. Shape + validation:
-- src/cqc_lem/utilities/brand_kit.py; docs/image-stack.md "Brand kit".
ALTER TABLE engagement_preferences
    ADD COLUMN brand_kit JSON NULL DEFAULT NULL;
