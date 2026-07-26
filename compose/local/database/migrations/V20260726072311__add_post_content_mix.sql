-- The 70/20/10 content-mix class of each planned post (issue #618): 'value' (audience value),
-- 'authority' (expertise education, no selling), or 'promo' (the one-in-ten case-study slot).
-- Assigned in code by the content-plan governor (content_alignment.assign_content_mix), it steers
-- generation AND is what the analytics dashboard measures mix compliance from. Nullable so existing
-- and manually-created posts stay unclassified — unclassified is never treated as promo.
ALTER TABLE posts ADD COLUMN content_mix VARCHAR(20) NULL AFTER buyer_stage;
