-- Edited-draft labels, Phase 1b (issue #2627): when the user edits a LEM draft before approving it,
-- the edit is the label. One row per (user, lane, item), created at the FIRST edit of a pending
-- draft with the text as it was drafted; `final_text` is filled at approval and only when it
-- differs from `original_text`. A draft approved untouched never gets a row.
--
-- Also widens voice_exemplars.source with 'edited' so an approved final can enter the corpus.
-- Additive: a new table and one appended ENUM member (existing values and order unchanged).
--
-- ROLLBACK (not run by Flyway): DROP TABLE voice_edit_labels;
--   (the 'edited' ENUM member can stay; delete rows with source='edited' first if it must go).

CREATE TABLE IF NOT EXISTS voice_edit_labels (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id INT NOT NULL,
    lane VARCHAR(24) NOT NULL,
    item_id BIGINT UNSIGNED NOT NULL,
    original_text TEXT NOT NULL,
    final_text TEXT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_at TIMESTAMP NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_voice_edit_labels_item (user_id, lane, item_id),
    CONSTRAINT fk_voice_edit_labels_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

ALTER TABLE voice_exemplars
    MODIFY COLUMN source ENUM('comments','shares','edited') NOT NULL;
