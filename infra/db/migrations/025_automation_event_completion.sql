-- Keep automation intent authoritative in PostgreSQL until the Redis consumer has
-- durably dispatched every matching Temporal workflow.

ALTER TABLE automation_events
    ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ;

ALTER TABLE automation_events
    DROP CONSTRAINT IF EXISTS automation_events_status_check;

ALTER TABLE automation_events
    ADD CONSTRAINT automation_events_status_check CHECK (
        status IN ('pending', 'publishing', 'published', 'processed')
    );

CREATE INDEX IF NOT EXISTS idx_automation_events_published_recovery
    ON automation_events(status, published_at)
    WHERE status = 'published';
