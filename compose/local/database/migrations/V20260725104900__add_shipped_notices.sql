-- "You asked, we shipped" changelog + reporter notification (issue #502), the last stage of the
-- feedback->auto-work loop: capture (#496) -> classify (#497) -> file (#498) -> SHIP + tell the
-- people who asked, then re-measure satisfaction.
--
-- shipped_notices           one row per GitHub issue that a merged PR closed, carrying the
--                           human-readable changelog line (the user-facing "what's new" feed).
-- shipped_notice_recipients who was told, when, whether they've seen it in-app. The PK is what
--                           makes "notified once" true across the email and the in-app channel.
--
-- The micro-CSAT that follows a notice ("did this fix it?") is asked through the existing
-- `survey_prompts` ledger (key `fix_csat_<issue>`) and ANSWERED as an ordinary `feedback` row, so
-- the answer re-enters the same classifier/clustering loop as any other report — hence the new
-- 'csat' source value.
CREATE TABLE IF NOT EXISTS shipped_notices (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    github_issue_number INT NOT NULL,
    pr_number           INT NULL,
    title               VARCHAR(255) NULL,
    changelog_line      VARCHAR(512) NOT NULL,
    shipped_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_shipped_notice_issue (github_issue_number),
    INDEX idx_shipped_notice_shipped_at (shipped_at)
);

CREATE TABLE IF NOT EXISTS shipped_notice_recipients (
    notice_id   INT NOT NULL,
    user_id     INT NOT NULL,
    notified_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    seen_at     DATETIME NULL,
    PRIMARY KEY (notice_id, user_id),
    INDEX idx_shipped_recipient_user (user_id, seen_at),
    FOREIGN KEY (notice_id) REFERENCES shipped_notices(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Additive ENUM value only — existing rows keep their source.
ALTER TABLE feedback
    MODIFY COLUMN source ENUM('widget','bug','nps','review','passive','csat')
    NOT NULL DEFAULT 'widget';
