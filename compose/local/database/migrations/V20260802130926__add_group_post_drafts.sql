-- Group-post preview + edit (issue #932). The weekly group post used to be written and published in
-- the same Selenium run, so the only thing a user ever saw about it was the per-group Post toggle —
-- never the text, and never a chance to revise it. The text is now written days AHEAD into this
-- table, where the SPA can read and edit it, and the publish run consumes the row instead of
-- generating anything.
--
-- One OPEN ('ready') row per user at a time: the draft beat skips a user who still has one, so an
-- unpublished draft is carried forward rather than silently replaced with a fresh generation the
-- user never asked for (their edits are in it).
--
-- Statuses: ready (drafted/edited — publishes at the weekly slot unless skipped), skipped (the user
-- cancelled this week's post, or its group stopped taking posts), published (it shipped),
-- failed (the run reached the group and the group would not take a member post).
CREATE TABLE IF NOT EXISTS group_post_drafts (
    id           INT NOT NULL AUTO_INCREMENT,
    user_id      INT NOT NULL,
    group_id     VARCHAR(64) NOT NULL,
    group_name   VARCHAR(255) NULL,
    content      TEXT NOT NULL,
    status       VARCHAR(20) NOT NULL DEFAULT 'ready',
    published_at TIMESTAMP NULL,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_group_post_drafts_user_status (user_id, status),
    CONSTRAINT group_post_drafts_user_fk FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
