-- Comment outcome tracking (issue #628). LEM posted comments and never looked back, so there was no
-- record of whether a comment earned a reply, a like, or was silently demoted out of LinkedIn's
-- default 'Most relevant' comment view — the May 2026 enforcement's quiet kill for automated-looking
-- comments. One row per comment we posted (log_id -> the logs row carrying our comment text), written
-- ONCE by the T+24h follow-up sweep.
--
-- UNIQUE (user_id, log_id) makes the check at-most-once: a skipped check (deleted/private post, our
-- comment not findable) still writes its row with status='skipped' + skip_reason, so an unfindable
-- comment is never re-walked every night.
--
-- visible_most_relevant is deliberately NULLABLE and three-valued: 1 = our comment was present under
-- the default 'Most relevant' sort, 0 = absent there but present under 'Most recent' (the demotion
-- signal), NULL = ambiguous (sort control not found / could not switch) — a guess here would poison
-- the demotion rate that gates commenting.
CREATE TABLE IF NOT EXISTS comment_outcomes (
    id                    INT NOT NULL AUTO_INCREMENT,
    user_id               INT NOT NULL,
    log_id                INT NOT NULL,
    post_key              VARCHAR(255) NULL,
    checked_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    author_replied        TINYINT(1) NOT NULL DEFAULT 0,
    reply_count           INT NOT NULL DEFAULT 0,
    like_count            INT NOT NULL DEFAULT 0,
    visible_most_relevant TINYINT(1) NULL,
    our_reply_sent        TINYINT(1) NOT NULL DEFAULT 0,
    status                VARCHAR(20) NOT NULL DEFAULT 'checked',
    skip_reason           VARCHAR(255) NULL,
    created_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_comment_outcomes_log (user_id, log_id),
    KEY idx_comment_outcomes_user_checked (user_id, checked_at),
    CONSTRAINT comment_outcomes_user_fk FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
    CONSTRAINT comment_outcomes_log_fk FOREIGN KEY (log_id) REFERENCES logs (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
