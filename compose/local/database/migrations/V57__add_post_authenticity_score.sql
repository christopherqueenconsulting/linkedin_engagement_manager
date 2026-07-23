-- Authenticity gate (issue #382 — 360Brew / LinkedIn 2026 Authenticity Update defense).
-- An LLM judge scores each generated post for generic-AI risk + profile-topic consistency
-- (0 = obviously generic AI slop, 100 = authentic, specific, on-voice). The content-plan
-- status-setter demotes an auto-approve to PENDING (human review) when the score falls below
-- AUTHENTICITY_SCORE_MIN — the same demote-not-block posture as the deterministic similarity gate.
-- NULL = not yet scored (e.g. pre-existing rows, or the gate disabled).
ALTER TABLE posts ADD COLUMN authenticity_score INT NULL;
