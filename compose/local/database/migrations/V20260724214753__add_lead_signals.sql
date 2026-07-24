-- Inbound-intent detection & hot-lead routing (issue #483): someone asking "how much?", "do you
-- help with X?", or "DM me" in a comment/reply/DM is the warmest lead we get, and today those
-- signals are read and dropped. This table is the surfaced queue: one row per detected signal,
-- deduped per (user_id, thread_key) so one conversation is never re-flagged.
--
-- source: which read path caught it — a comment on OUR post, a reply to OUR automated comment, or
--         an inbound DM (all rides on reads that already happen; no new scraping).
-- channel: how the approved response goes out — 'reply' posts under their comment at context_url,
--          'dm' sends the drafted message via the existing send_private_dm primitive.
-- status: new -> draft awaiting human approval
--         approved -> human approved; the responder task will deliver it
--         sent -> the response was delivered
--         dismissed -> operator dismissed the signal (no response)
--         failed -> delivery errored
CREATE TABLE IF NOT EXISTS lead_signals (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    user_id             INT NOT NULL,
    source              ENUM('post_comment','comment_reply','dm') NOT NULL,
    channel             ENUM('reply','dm') NOT NULL DEFAULT 'reply',
    person_name         VARCHAR(255) NULL,
    person_profile_url  VARCHAR(512) NULL,
    thread_key          VARCHAR(255) NOT NULL,   -- dedup anchor: person + thread, never raw text
    snippet             TEXT NULL,               -- what they actually said (evidence for the operator)
    score               TINYINT UNSIGNED NOT NULL DEFAULT 0,
    matched_signals     VARCHAR(512) NULL,       -- comma-separated intent labels that fired
    post_id             INT NULL,                -- our post, when the signal came from one
    context_url         VARCHAR(512) NULL,       -- post/thread URL to reply in
    draft_response      TEXT NULL,               -- approval-gated draft, editable before sending
    status              ENUM('new','approved','sent','dismissed','failed') NOT NULL DEFAULT 'new',
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_lead_signal_thread (user_id, thread_key),
    KEY idx_lead_signal_user_status (user_id, status),
    KEY idx_lead_signal_created (user_id, created_at),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
