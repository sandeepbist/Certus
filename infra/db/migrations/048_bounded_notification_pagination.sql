-- Keep filtered notification traversal on tenant-scoped keyset indexes. The
-- unfiltered feed continues to use idx_notifications_tenant_user_created.
CREATE INDEX IF NOT EXISTS idx_notifications_tenant_user_read_page
    ON notifications(organization_id, user_id, is_read, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_notifications_tenant_user_type_page
    ON notifications(organization_id, user_id, type, created_at DESC, id DESC);
