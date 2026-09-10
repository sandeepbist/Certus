CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_webhook_page
    ON webhook_deliveries(webhook_id, attempted_at DESC, id DESC);
