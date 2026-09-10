-- Stable keyset scans for long-lived workspace collections.

CREATE INDEX IF NOT EXISTS idx_memories_active_page
    ON memories (
        tenant_id,
        user_id,
        confidence DESC,
        created_at DESC,
        id DESC
    )
    WHERE is_active = true;

CREATE INDEX IF NOT EXISTS idx_automation_rules_page
    ON automation_rules (tenant_id, user_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_automation_executions_rule_page
    ON automation_executions (
        tenant_id,
        user_id,
        rule_id,
        started_at DESC,
        id DESC
    );
