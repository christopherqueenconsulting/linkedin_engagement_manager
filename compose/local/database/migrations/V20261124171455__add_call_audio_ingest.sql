-- Call ingest Phase 2 (issue #2729): audio intake. Additive only.
--
-- call_source.source_type   gains 'audio' (a recording the user uploads).
-- call_ingest               `audio_path` is the spooled recording until the transcript is written
--                           (then NULLed and the file deleted); `audio_purge_after` is the 24 h cap
--                           for a transcription that never finishes.
-- call_voice_enrollment     the user's own-voice embedding (sealed, AAD `call_ingest.voice_embedding`),
--                           one row per user, deleted by purge_all_call_data.
ALTER TABLE call_source
    MODIFY COLUMN source_type ENUM('otter','wispr','audio') NOT NULL;

ALTER TABLE call_ingest
    ADD COLUMN audio_path VARCHAR(512) NULL,
    ADD COLUMN audio_purge_after DATETIME NULL,
    ADD INDEX idx_call_ingest_audio_purge (audio_purge_after);

CREATE TABLE IF NOT EXISTS call_voice_enrollment (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    embedding_enc MEDIUMTEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_call_voice_enrollment_user (user_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
