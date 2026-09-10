-- Keep recovery and retention work proportional to eligible queue rows as the
-- document library accumulates years of processing history.

CREATE INDEX IF NOT EXISTS idx_document_embedding_jobs_published_recovery
    ON document_embedding_jobs(status, published_at)
    WHERE status = 'published';

CREATE INDEX IF NOT EXISTS idx_document_embedding_jobs_terminal_retention
    ON document_embedding_jobs(status, processed_at)
    WHERE status IN ('processed', 'failed', 'obsolete');

CREATE INDEX IF NOT EXISTS idx_automation_events_processed_retention
    ON automation_events(status, processed_at)
    WHERE status = 'processed';
