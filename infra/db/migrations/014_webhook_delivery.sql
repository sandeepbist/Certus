ALTER TABLE webhooks
    ADD COLUMN IF NOT EXISTS name VARCHAR(100),
    ADD COLUMN IF NOT EXISTS secret_hint VARCHAR(8),
    ADD COLUMN IF NOT EXISTS last_error TEXT,
    ADD COLUMN IF NOT EXISTS version INT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

UPDATE webhooks SET name = 'Webhook' WHERE name IS NULL;

ALTER TABLE webhooks
    ALTER COLUMN name SET NOT NULL;

ALTER TABLE webhook_deliveries
    ADD COLUMN IF NOT EXISTS attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS duration_ms INT,
    ADD COLUMN IF NOT EXISTS error_message TEXT,
    ADD COLUMN IF NOT EXISTS attempt_number INT NOT NULL DEFAULT 1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_webhooks_tenant_user_url_unique
    ON webhooks(organization_id, user_id, url);

CREATE INDEX IF NOT EXISTS idx_webhooks_tenant_user_enabled
    ON webhooks(organization_id, user_id, is_enabled, created_at DESC);

