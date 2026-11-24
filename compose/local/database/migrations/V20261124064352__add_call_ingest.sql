-- Call / voice-note ingest, Phase 1a (issue #2629): the intake, provenance and review tables that
-- let a user's own Otter transcripts and Wispr Flow notes become story-bank facts.
--
-- call_source        a registered source with its consent attestation ("everyone on this call was
--                    told it was recorded"). Per-source opt-in: off until attested.
-- call_ingest        one transcript. `transcript_enc` is the REDACTED transcript sealed with the
--                    envelope scheme (AAD `call_ingest.transcript`); it is nulled when purged and
--                    the row stays as the audit trail. `purge_after` is the hard cap.
-- extract_candidate  a verified fact awaiting review. Nothing reaches generation while `pending`.
-- story_bank         nullable provenance columns plus `review_status`. Selectors read only
--                    review_status='accepted' AND active=1; existing rows default to 'accepted'.
CREATE TABLE IF NOT EXISTS call_source (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    source_type ENUM('otter','wispr') NOT NULL,
    label VARCHAR(120) NULL,
    consent_attested TINYINT(1) NOT NULL DEFAULT 0,
    consent_attested_at DATETIME NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_call_source_user (user_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS call_ingest (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    source_id INT NULL,
    title VARCHAR(255) NULL,
    content_hash CHAR(64) NOT NULL,
    transcript_enc MEDIUMTEXT NULL,
    own_speaker VARCHAR(120) NULL,
    turn_count INT NOT NULL DEFAULT 0,
    status ENUM('received','extracted','reviewed','purged','failed') NOT NULL DEFAULT 'received',
    purge_after DATETIME NOT NULL,
    purged_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_call_ingest_user_hash (user_id, content_hash),
    INDEX idx_call_ingest_purge (purge_after, purged_at),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (source_id) REFERENCES call_source(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS extract_candidate (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    ingest_id INT NOT NULL,
    kind ENUM('anecdote','number','opinion','client_win','mistake','artifact') NOT NULL DEFAULT 'anecdote',
    title VARCHAR(255) NOT NULL,
    body TEXT NOT NULL,
    source_quote TEXT NOT NULL,
    t0 DECIMAL(10,2) NULL,
    confidence DECIMAL(4,3) NOT NULL DEFAULT 0,
    status ENUM('pending','accepted','edited','rejected') NOT NULL DEFAULT 'pending',
    story_bank_id INT NULL,
    reviewed_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_extract_candidate_user_status (user_id, status),
    INDEX idx_extract_candidate_ingest (ingest_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (ingest_id) REFERENCES call_ingest(id) ON DELETE CASCADE
);

ALTER TABLE story_bank
    ADD COLUMN origin VARCHAR(32) NULL,
    ADD COLUMN ingest_id INT NULL,
    ADD COLUMN source_quote TEXT NULL,
    ADD COLUMN candidate_id INT NULL,
    ADD COLUMN review_status ENUM('pending','accepted','rejected') NOT NULL DEFAULT 'accepted';
