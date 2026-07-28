-- Rejection reason field for posts (issue #713).
-- When a user rejects/deletes a draft, capture WHY so a later regeneration can avoid the same issue.
-- Nullable TEXT so existing rows and soft-deletes without a reason backfill cleanly.
ALTER TABLE posts
    ADD COLUMN rejection_reason TEXT NULL AFTER gate_reason;
