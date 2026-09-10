-- Persist locally generated GDPR archives until their explicit expiry. Production
-- deployments can later replace the BYTEA payload with an object-storage URL
-- without changing the authenticated export lifecycle.

ALTER TABLE data_exports
    ADD COLUMN IF NOT EXISTS organization_id TEXT,
    ADD COLUMN IF NOT EXISTS format VARCHAR(100),
    ADD COLUMN IF NOT EXISTS file_name TEXT,
    ADD COLUMN IF NOT EXISTS size_bytes BIGINT,
    ADD COLUMN IF NOT EXISTS archive BYTEA,
    ADD COLUMN IF NOT EXISTS record_counts JSONB NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS downloaded_at TIMESTAMPTZ;

UPDATE data_exports AS export
SET organization_id = preferences.organization_id
FROM user_preferences AS preferences
WHERE export.organization_id IS NULL
  AND preferences.user_id = export.user_id
  AND preferences.organization_id IS NOT NULL;

DELETE FROM data_exports WHERE organization_id IS NULL;

ALTER TABLE data_exports
    ALTER COLUMN organization_id SET NOT NULL;

ALTER TABLE data_exports
    DROP CONSTRAINT IF EXISTS data_exports_status_check,
    ADD CONSTRAINT data_exports_status_check
        CHECK (status IN ('pending', 'processing', 'ready', 'expired', 'failed')),
    DROP CONSTRAINT IF EXISTS data_exports_size_check,
    ADD CONSTRAINT data_exports_size_check
        CHECK (size_bytes IS NULL OR size_bytes >= 0);

CREATE INDEX IF NOT EXISTS idx_data_exports_tenant_user_created
    ON data_exports(organization_id, user_id, created_at DESC);
