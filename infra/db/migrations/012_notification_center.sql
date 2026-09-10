-- Support tenant-scoped chronological notification feeds in addition to the
-- existing partial index used for unread-count lookups.
CREATE INDEX IF NOT EXISTS idx_notifications_tenant_user_created
    ON notifications(organization_id, user_id, created_at DESC, id DESC);

