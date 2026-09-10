-- Support bounded cleanup of successfully published real-time delivery state.
-- Pending publication rows are intentionally excluded and retained indefinitely.

CREATE INDEX IF NOT EXISTS idx_notification_events_published_retention
    ON notification_events(published_at, id)
    WHERE status = 'published';
