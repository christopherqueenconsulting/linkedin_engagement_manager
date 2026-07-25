-- Durable, Stripe-joinable cost store behind the margin report (issue #490,
-- docs/cost-performance-margin-plan.md §A.3(3)). Mirrors the video_credit_ledger pattern:
-- append-only rows, summed per user/feature/provider. PostHog stays the fast analytics plane;
-- this table is the exact source of truth (PostHog data can be sampled/retention-limited).
-- user_id NULL = system/shared cost that is not attributable to one user.
CREATE TABLE IF NOT EXISTS cost_ledger (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id      INT           NULL,
    feature      VARCHAR(32)   NOT NULL,  -- content|comment|dm|newsletter|research|image|video|system
    category     VARCHAR(32)   NOT NULL,  -- llm|media|proxy|email|geocoding|infra|posthog
    provider     VARCHAR(64)   NULL,      -- openai|anthropic|ollama|runway|perplexity|sendgrid|...
    model_tier   VARCHAR(64)   NULL,      -- lem-* alias (LLM) or the concrete media model
    usd          DECIMAL(12,6) NOT NULL,  -- fine precision: cheap tiers round to 0 at fewer decimals
    qty          DECIMAL(12,4) NULL,      -- tokens / seconds / images / sends / lookups
    post_id      INT           NULL,
    task_name    VARCHAR(128)  NULL,
    incurred_on  DATE          NOT NULL,  -- for daily/monthly rollups
    created_at   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_cost_user_day (user_id, incurred_on),
    KEY idx_cost_feature_day (feature, incurred_on),
    KEY idx_cost_category_day (category, incurred_on),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);
