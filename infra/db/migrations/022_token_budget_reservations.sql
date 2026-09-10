-- Reserve daily token capacity before an agent run begins so concurrent runs
-- cannot all spend the same remaining budget.

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS reserved_tokens INT NOT NULL DEFAULT 0;

UPDATE agent_runs
SET reserved_tokens = 0
WHERE reserved_tokens < 0;

ALTER TABLE agent_runs
    DROP CONSTRAINT IF EXISTS agent_runs_reserved_tokens_check;
ALTER TABLE agent_runs
    ADD CONSTRAINT agent_runs_reserved_tokens_check
    CHECK (reserved_tokens >= 0);

UPDATE tenant_config
SET max_token_budget_daily = 100000
WHERE max_token_budget_daily IS NULL OR max_token_budget_daily <= 0;

ALTER TABLE tenant_config
    ALTER COLUMN max_token_budget_daily SET DEFAULT 100000,
    ALTER COLUMN max_token_budget_daily SET NOT NULL;

ALTER TABLE tenant_config
    DROP CONSTRAINT IF EXISTS tenant_config_token_budget_check;
ALTER TABLE tenant_config
    ADD CONSTRAINT tenant_config_token_budget_check
    CHECK (max_token_budget_daily > 0 AND max_token_budget_daily <= 100000000);

CREATE INDEX IF NOT EXISTS idx_agent_runs_daily_token_usage
    ON agent_runs(tenant_id, user_id, created_at DESC)
    INCLUDE (status, total_tokens, reserved_tokens);
