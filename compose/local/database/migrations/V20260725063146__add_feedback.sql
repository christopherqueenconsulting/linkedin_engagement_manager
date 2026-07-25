-- In-app feedback capture (issue #496): the first capture point of the feedback->auto-work loop.
-- The SPA widget POSTs free text + a type hint + auto-attached context (route, app version, PostHog
-- session id, optional screenshot) here. Later steps of the loop fill the enrichment columns:
--   embedding           -> vector for clustering similar reports (stored as a JSON array)
--   cluster_id          -> the cluster this item was grouped into
--   github_issue_number -> the issue auto-opened for its cluster
--   sentiment           -> classified tone of the body
-- user_id is NULLABLE on purpose: the widget is shown to logged-out visitors too.
CREATE TABLE IF NOT EXISTS feedback (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NULL,
    source ENUM('widget','bug','nps','review','passive') NOT NULL DEFAULT 'widget',
    type_hint VARCHAR(32) NULL,
    body TEXT NOT NULL,
    context_json JSON NULL,
    embedding JSON NULL,
    cluster_id INT NULL,
    github_issue_number INT NULL,
    status ENUM('new','triaged','clustered','issue_created','resolved','dismissed') NOT NULL DEFAULT 'new',
    sentiment VARCHAR(16) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_feedback_user (user_id, created_at),
    INDEX idx_feedback_status (status, created_at),
    INDEX idx_feedback_cluster (cluster_id)
);
