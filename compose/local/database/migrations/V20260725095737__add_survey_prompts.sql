-- NPS/CSAT + review prompts (issue #501). The RESPONSES live in `feedback` (source='nps'/'review')
-- so they flow through the same classifier/clustering loop as every other piece of feedback; this
-- table only records that we ASKED, so each survey is one-shot per user (no daily re-ask) and the
-- in-app modal and the email prompt share one ledger.
CREATE TABLE IF NOT EXISTS survey_prompts (
    user_id     INT NOT NULL,
    survey_key  VARCHAR(32) NOT NULL,
    sent_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, survey_key),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
