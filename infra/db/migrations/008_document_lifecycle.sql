-- Add recoverable document deletion while allowing a user to upload the same
-- content again after removing the previous copy.

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

DROP INDEX IF EXISTS idx_documents_tenant_user_hash_unique;

CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_tenant_user_hash_active_unique
    ON documents(tenant_id, user_id, content_hash)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_documents_active_tenant_user
    ON documents(tenant_id, user_id, created_at DESC)
    WHERE deleted_at IS NULL;
