-- Issue #2116: record WHO moved a post to 'approved'.
--
-- Post 105 was held on 2026-09-13 and published on 2026-09-17 with a slop HARD finding and an
-- authenticity score of 55. Nothing on the row, in the logs table, or in the log file said what
-- approved it — a user click, the auto-schedule gate, a re-score, or one of the asset-heal paths
-- that promote a post straight to 'approved'.
--
-- `approved_by` is the actor of the most recent transition INTO 'approved': 'user:<id>' for an
-- author action through the API, 'system:<source>' for an automatic path (the `PostApprover`
-- enum). `approved_at` is when that transition happened, in naive UTC like every other time
-- column. A post re-queued after a deferred publish keeps the actor that approved it.
--
-- Both NULL on every existing row: those approvals were never recorded, and inventing an actor for
-- them would be worse than admitting it.
ALTER TABLE posts
    ADD COLUMN approved_by VARCHAR(64) NULL,
    ADD COLUMN approved_at DATETIME NULL;
