-- Monthly ICP-audience report storage (issue #2758). Additive only.
--
-- icp_audience_reports: ONE row per user per month. period_key is '2026-11'; the UNIQUE key is
-- what makes a beat re-run a no-op. report_json is the full load_icp_audience_report payload.
CREATE TABLE IF NOT EXISTS icp_audience_reports (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id     INT NOT NULL,
    period_key  VARCHAR(7) NOT NULL,
    report_json LONGTEXT NOT NULL,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_icp_audience_reports_user_period (user_id, period_key),
    CONSTRAINT fk_icp_audience_reports_user
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
