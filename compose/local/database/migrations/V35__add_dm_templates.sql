-- Per-user, per-event DM templates the LLM renders in the user's voice (replaces the
-- hard-coded appreciation / profile-viewer messages). step=0 is the initial message;
-- step>=1 are follow-ups (delay_hours from the previous step) consumed by the follow-up
-- sequencer. Missing row → code-level default templates (today's strings), so behaviour is
-- unchanged until a user customizes. template_text supports {first_name},{headline},{blog_url}.
CREATE TABLE IF NOT EXISTS dm_templates (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    user_id       INT NOT NULL,
    event_type    ENUM('connection_accepted','recommendation_received',
                       'collaboration','profile_viewer','manual') NOT NULL,
    step          INT NOT NULL DEFAULT 0,
    delay_hours   INT NOT NULL DEFAULT 0,
    template_text TEXT NOT NULL,
    is_active     TINYINT(1) NOT NULL DEFAULT 1,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_user_event_step (user_id, event_type, step),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
