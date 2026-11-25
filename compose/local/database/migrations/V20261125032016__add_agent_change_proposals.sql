-- MCP epic #2797, S5 (issue #2803; the P7 proposals table of #2691, scoped to settings). Additive only.
--
-- agent_change_proposals   a change an AGENT asked for and only the USER may apply: one row per
--                          request, `payload` = {field: {before, requested}}. Nothing in this table
--                          changes a setting; approving one is a separate, step-up gated route.
--   reason                 the agent's own text: UNTRUSTED, shown quoted and never rendered as HTML.
--   status                 pending | approved | rejected | expired (a VARCHAR, so no ENUM to widen).
--   source_session_id      the MCP token's session that filed it; no FK, a revoked token's rows stay.
--   outcome                after an approval: {applied: [...], stale: [...], declined: [...]}.
--   expires_at             created_at + 7 days; an expired row can never be approved.
CREATE TABLE IF NOT EXISTS agent_change_proposals (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    kind VARCHAR(40) NOT NULL DEFAULT 'settings',
    payload JSON NOT NULL,
    reason TEXT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    source_session_id INT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME NOT NULL,
    decided_at DATETIME NULL,
    decided_by_session_id INT NULL,
    outcome JSON NULL,
    KEY idx_agent_change_proposals_user_status (user_id, status, expires_at),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
