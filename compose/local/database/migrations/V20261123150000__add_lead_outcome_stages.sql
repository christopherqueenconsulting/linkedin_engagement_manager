-- Outcome tracking (issue #2633, Phase 1): the pipeline could not show the result LEM exists for.
-- `leads.stage` stays the COMPUTED warmth (derive_stage); the outcome stages are an OPERATOR call,
-- so they widen `manual_stage` only — the nightly rebuild never writes it and can never undo one.
-- The ENUM restates every existing value and appends the four new ones.
ALTER TABLE leads
    MODIFY COLUMN manual_stage ENUM('cold','warm','hot','in_conversation','opportunity',
                                    'meeting_booked','meeting_held','won','lost') NULL,
    ADD COLUMN deal_value DECIMAL(12,2) NULL AFTER notes;

-- One row per stage change, so the funnel is a history and not just current state.
-- source: 'manual' (operator button), 'derived' (nightly re-score moved the computed stage),
--         'booking' (reserved for the booking hook — a later phase).
-- from_stage is NULL the first time a lead is seen. evidence_ref names what justified the move.
CREATE TABLE IF NOT EXISTS lead_stage_events (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    lead_id       INT NOT NULL,
    user_id       INT NOT NULL,
    from_stage    VARCHAR(32) NULL,
    to_stage      VARCHAR(32) NOT NULL,
    source        ENUM('manual','derived','booking') NOT NULL,
    evidence_ref  VARCHAR(255) NULL,
    at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_lead_stage_events_user_at (user_id, at),
    KEY idx_lead_stage_events_lead (lead_id, at),
    FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
