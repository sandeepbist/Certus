-- Stable tenant-scoped trace traversal for the unfiltered feed and its two
-- supported equality filters. Full-text query filtering uses migration 013's
-- stored GIN search projection before the keyset boundary is applied.
CREATE INDEX IF NOT EXISTS idx_agent_runs_tenant_user_page
    ON agent_runs(tenant_id, user_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_agent_runs_tenant_user_model_page
    ON agent_runs(tenant_id, user_id, model_used, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_agent_runs_tenant_user_status_page
    ON agent_runs(tenant_id, user_id, status, created_at DESC, id DESC);
