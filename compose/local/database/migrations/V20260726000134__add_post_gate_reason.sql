-- Why a draft is PENDING (issue #421). A quality gate (A1 authenticity below threshold, the
-- near-duplicate check, missing media) demotes APPROVED -> PENDING, but the review queue had no way
-- to show WHICH gate fired, the score it fired on, or what to do about it. Each gate now records a
-- structured finding (gate, score, threshold, explanation, remediation, demoted) and they are stored
-- here as a JSON array, alongside the raw scores in authenticity_score (V57) / dwell_score.
-- NULL = no gate has evaluated this post, or it passed everything.
ALTER TABLE posts ADD COLUMN gate_reason JSON NULL;

-- The gate thresholds, per user. Both were deploy-wide env vars only (AUTHENTICITY_SCORE_MIN /
-- POST_SIMILARITY_MAX), so a user who found the gates too strict (or too lax) for their risk
-- appetite had no way to tune them. NULL = follow the env/code default, so behaviour is unchanged
-- until the user sets one. Similarity is stored as a whole percent (the unit the SPA edits) and
-- converted to the 0-1 overlap fraction the gate compares against.
ALTER TABLE engagement_preferences
    ADD COLUMN authenticity_score_min INT NULL AFTER personal_goals,
    ADD COLUMN post_similarity_max_pct INT NULL AFTER authenticity_score_min;
