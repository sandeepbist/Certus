-- Stable active-library traversal for the unfiltered feed and supported
-- equality/array filters. Title search continues to use migration 013's
-- partial GIN projection.
CREATE INDEX IF NOT EXISTS idx_documents_active_tenant_user_page
    ON documents(tenant_id, user_id, created_at DESC, id DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_documents_active_tenant_user_status_page
    ON documents(tenant_id, user_id, status, created_at DESC, id DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_documents_active_tenant_user_source_page
    ON documents(tenant_id, user_id, source_type, created_at DESC, id DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_documents_active_tags
    ON documents USING gin(tags)
    WHERE deleted_at IS NULL;
