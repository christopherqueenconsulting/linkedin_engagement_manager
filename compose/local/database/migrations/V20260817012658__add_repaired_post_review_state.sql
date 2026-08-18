-- The repair pass's durable residue, and the toggle that decides what it costs (issue #1134).
--
-- ever_gate_demoted  set ONCE, only when `_review_generated_post` sent a draft to the editor for
--                    repair because a deterministic check (similarity, personal proof, fabrication,
--                    fact grounding, AI-slop) failed on it. It is NOT "this post is held" — the
--                    hold lives in gate_reason and is recomputed every pass — it is "this text was
--                    fixed rather than written clean", which no later pass can re-derive: the
--                    failing draft is gone by the time the gates run.
--                    NOT NULL DEFAULT 0 because every post that predates this column was written
--                    before the repair path existed, so "never repaired" is the honest reading.
--
-- hold_repaired_posts_for_review  per-user, DEFAULT 1 (ON). A repaired post that now passes every
--                    gate is still held PENDING for the author, because the checks passing on the
--                    second draft is exactly the case where nobody ever read the first. Off restores
--                    the pre-#1134 behaviour verbatim: auto_schedule_posts alone decides.
--                    NOT NULL DEFAULT 1 makes "never chosen" and "left on" identical, the same
--                    reading roster_auto_follow's NOT NULL DEFAULT 0 gives its own default.
ALTER TABLE posts
    ADD COLUMN ever_gate_demoted TINYINT(1) NOT NULL DEFAULT 0;

ALTER TABLE engagement_preferences
    ADD COLUMN hold_repaired_posts_for_review TINYINT(1) NOT NULL DEFAULT 1;
