-- Stable keyset scans for task histories, including status-filtered pages.

CREATE INDEX IF NOT EXISTS idx_tasks_tenant_user_page
    ON tasks (
        tenant_id,
        user_id,
        created_at DESC,
        id DESC
    );

CREATE INDEX IF NOT EXISTS idx_tasks_tenant_user_status_page
    ON tasks (
        tenant_id,
        user_id,
        status,
        created_at DESC,
        id DESC
    );
