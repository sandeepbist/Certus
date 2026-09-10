-- Preserve sub-microdollar provider estimates for small grounded-answer runs.
-- The application records a versioned pricing breakdown in generation_profile;
-- this projection remains nullable when a model or service tier is unpriced.

ALTER TABLE agent_runs
    ALTER COLUMN estimated_cost_usd TYPE NUMERIC(14, 8),
    DROP CONSTRAINT IF EXISTS agent_runs_estimated_cost_nonnegative_check,
    ADD CONSTRAINT agent_runs_estimated_cost_nonnegative_check CHECK (
        estimated_cost_usd IS NULL OR estimated_cost_usd >= 0
    );

COMMENT ON COLUMN agent_runs.estimated_cost_usd IS
    'Nullable estimated USD cost rounded to 8 decimals; exact pricing provenance is stored in generation_profile.';
