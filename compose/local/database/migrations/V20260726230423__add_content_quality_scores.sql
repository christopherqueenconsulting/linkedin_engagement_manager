-- Content-quality telemetry (issue #630 / D6). ONE row per piece of content LEM shipped, scored the
-- night it went out, so quality has a TREND LINE instead of a one-time verdict.
--
-- Why a table and not just PostHog events: the weekly rollup has to compare this week against last
-- week to catch a regression after a model or prompt swap. Re-scoring last week's content every
-- Monday would double the embedding spend AND give a different answer whenever a threshold changed;
-- reading back the rows that were already scored gives the same number twice. The PostHog events are
-- the dashboard/alerting surface, this is the record they were derived from.
--
-- Everything measured is NULLABLE on purpose: an unscored dimension (no impressions yet, no stored
-- authenticity score, embeddings unavailable) must read as "not measured", never as a zero — the
-- same rule comment_outcomes.visible_most_relevant follows.
CREATE TABLE IF NOT EXISTS content_quality_scores (
    id                 INT AUTO_INCREMENT PRIMARY KEY,
    user_id            INT NOT NULL,
    surface            VARCHAR(20) NOT NULL,          -- post | comment | newsletter
    ref_id             VARCHAR(64) NOT NULL,          -- posts.id / logs.id / newsletter_editions.id
    shipped_on         DATE NOT NULL,
    slop_hard          INT NULL,
    slop_warn          INT NULL,
    slop_score         DECIMAL(7,3) NULL,
    similarity         DECIMAL(6,4) NULL,
    similarity_measure VARCHAR(16) NULL,
    authenticity_score INT NULL,
    hook_chars         INT NULL,
    hook_within_budget TINYINT(1) NULL,
    engagement_rate    DECIMAL(12,8) NULL,
    impressions        INT NULL,
    detector_score     DECIMAL(6,4) NULL,
    detector_provider  VARCHAR(32) NULL,
    checks             JSON NULL,                     -- the slop checks that fired, for explainability
    scored_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_content_quality (user_id, surface, ref_id),
    KEY idx_user_shipped (user_id, shipped_on),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
