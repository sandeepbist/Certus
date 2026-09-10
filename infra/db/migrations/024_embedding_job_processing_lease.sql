-- Prevent duplicate at-least-once Redis deliveries from invoking the embedding
-- provider concurrently. The existing locked_at column becomes a recoverable
-- worker lease after publication.

ALTER TABLE document_embedding_jobs
    DROP CONSTRAINT IF EXISTS document_embedding_jobs_status_check;

ALTER TABLE document_embedding_jobs
    ADD CONSTRAINT document_embedding_jobs_status_check CHECK (
        status IN (
            'pending', 'publishing', 'published', 'processing',
            'processed', 'failed', 'obsolete'
        )
    );

CREATE INDEX IF NOT EXISTS idx_document_embedding_jobs_processing_lease
    ON document_embedding_jobs(status, locked_at)
    WHERE status = 'processing';
