-- Issue #2026: the roster rotation starves on its own sort key.
--
-- `select_roster_targets` orders by `_target_staleness`, which returns the IDENTICAL key (0, 0.0)
-- for every never-engaged target. Python's sort is stable, so the order among them is DB order —
-- the same order on every run — and the run budget takes the head of that list each time. A target
-- only leaves the head when a comment actually LANDS on it and stamps `last_engaged_at`.
--
-- Measured 2026-09-10, three days of production logs: `aurimas-griciunas` was visited and skipped
-- 64 times, `arazvant` 51, `migueloteropedrido` 29 — while 67 of the 82 curated targets were never
-- visited at all, and 73 have never been engaged. Those four are exactly the first four
-- never-engaged rows in id order.
--
-- Recording the VISIT is what breaks the loop: a target we looked at and found nothing commentable
-- on has still had its turn, and must go to the back of the queue. Independent of the on-topic gate
-- fix in the same issue — a target whose posts are genuinely off-domain would otherwise keep the
-- head of the list forever even with a working gate.
ALTER TABLE engagement_targets
    ADD COLUMN last_visited_at DATETIME NULL DEFAULT NULL AFTER last_engaged_at;

-- Seed from the engagement stamp so the first run after deploy does not treat every previously
-- engaged target as never-visited and re-walk the whole roster at once. Rows that were never
-- engaged stay NULL, which is correct: they genuinely have not had a turn.
UPDATE engagement_targets SET last_visited_at = last_engaged_at WHERE last_engaged_at IS NOT NULL;
