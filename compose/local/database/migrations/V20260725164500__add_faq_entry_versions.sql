-- Answer history for the auto-FAQ service (issue #507). The auto-FAQ pass rewrites published
-- answers with no human triage, so every write it makes must be revertible: one row per state the
-- entry has ever been in, newest last.
--
--   source -> who wrote this state: 'auto' (the auto-FAQ pass), 'revert' (an older version
--             re-applied), 'manual' (a human edit recorded through the service).
--   status -> the entry's status at the time this version was written, so reverting restores the
--             publication posture too, not just the copy.
CREATE TABLE IF NOT EXISTS faq_entry_versions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    faq_entry_id INT NOT NULL,
    question VARCHAR(512) NOT NULL,
    answer TEXT NOT NULL,
    status ENUM('published','draft','archived') NOT NULL DEFAULT 'draft',
    source VARCHAR(32) NOT NULL DEFAULT 'auto',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_faq_versions_entry (faq_entry_id, id)
);
