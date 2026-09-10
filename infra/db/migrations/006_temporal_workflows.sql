-- Durable reminder and automation execution state mirrored from Temporal.

CREATE TABLE IF NOT EXISTS reminders (
    id UUID PRIMARY KEY,
    user_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL UNIQUE,
    workflow_run_id TEXT,
    notification_id UUID NOT NULL UNIQUE,
    message TEXT NOT NULL,
    scheduled_for TIMESTAMPTZ NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'starting',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT reminders_status_check
        CHECK (status IN ('starting', 'scheduled', 'delivering', 'delivered', 'cancelled', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_reminders_tenant_user_schedule
    ON reminders(tenant_id, user_id, scheduled_for DESC);

ALTER TABLE automation_rules
    ADD COLUMN IF NOT EXISTS version INT NOT NULL DEFAULT 1;

ALTER TABLE automation_rules
    DROP CONSTRAINT IF EXISTS automation_rules_version_check,
    ADD CONSTRAINT automation_rules_version_check CHECK (version > 0);

CREATE TABLE IF NOT EXISTS automation_executions (
    id UUID PRIMARY KEY,
    rule_id UUID NOT NULL REFERENCES automation_rules(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    trigger_event VARCHAR(100) NOT NULL,
    trigger_event_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL UNIQUE,
    workflow_run_id TEXT,
    source_document_id UUID REFERENCES documents(id) ON DELETE SET NULL,
    created_task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'scheduled',
    result JSONB NOT NULL DEFAULT '{}',
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(rule_id, trigger_event_id),
    CONSTRAINT automation_executions_status_check
        CHECK (status IN ('scheduled', 'running', 'completed', 'skipped', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_automation_executions_tenant_user
    ON automation_executions(tenant_id, user_id, started_at DESC);
