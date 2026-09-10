-- Issue #2023: tell OUR OWN engagement apart from the audience's, and stop writing 0 for a
-- reading we never took.
--
-- `post_stats.comments` is the count the page renders, and our own two comments — the #344 seed
-- and the #622 second wave — are always part of it. Measured on 2026-09-10 across the last 20
-- published posts: 19 of them carry exactly 2 comments, both ours. Every dashboard, every
-- `get_shape_performance` archetype weight and every content-quality trend line has therefore been
-- reading a number that is structurally >= 2 and almost entirely us. The account reads ~2.4%
-- engagement on that number and ~0.5% on third-party engagement alone.
--
-- `own_comments` is NULL-able because it is a READING, not a derived constant: a capture that
-- could not count our own comments must be distinguishable from a post where we left none.
-- Third-party comments stay DERIVED (`comments - own_comments`) rather than stored, so the two
-- numbers can never drift apart in the row.
ALTER TABLE post_stats
    ADD COLUMN own_comments INT NULL DEFAULT NULL AFTER comments;

-- `saves` had no unknown state: `NOT NULL DEFAULT 0` meant a saves label that never rendered and a
-- genuine zero wrote the identical row, which makes the claim unfalsifiable from the database.
-- `impressions` on this same table is already NULL-able for exactly that reason, and the repo's
-- standing rule is that a missing reading is NULL, never 0 (docs/content-quality-telemetry.md).
-- Saves are the 5x-weighted 2026 ranking signal and `prefer_save_targeted` (#619) steers archetype
-- selection off this column, so a fabricated zero is not harmless.
--
-- Existing rows keep their 0. They were written by a reader that could not tell the difference, so
-- back-filling them to NULL would be inventing a second claim about the past — and a live probe on
-- 2026-09-10 read `Saves 0` off the analytics page, meaning at least some of those zeros are real.
ALTER TABLE post_stats
    MODIFY COLUMN saves INT NULL DEFAULT NULL;
