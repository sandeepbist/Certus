-- Make document processing crash-safe without duplicating chunk content in queue
-- payloads. PostgreSQL owns the source chunks and small transactional outbox rows;
-- Redis remains the at-least-once delivery transport.

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS processing_generation UUID;

UPDATE documents
SET processing_generation = uuid_generate_v4()
WHERE processing_generation IS NULL;

ALTER TABLE documents
    ALTER COLUMN processing_generation SET DEFAULT uuid_generate_v4(),
    ALTER COLUMN processing_generation SET NOT NULL;

ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS processing_generation UUID,
    ADD COLUMN IF NOT EXISTS embedded_at TIMESTAMPTZ;

UPDATE chunks AS chunk
SET processing_generation = document.processing_generation,
    embedded_at = CASE
        WHEN chunk.embedding IS NOT NULL THEN COALESCE(chunk.embedded_at, chunk.created_at, NOW())
        ELSE chunk.embedded_at
    END
FROM documents AS document
WHERE document.id = chunk.document_id
  AND chunk.processing_generation IS NULL;

ALTER TABLE chunks
    ALTER COLUMN processing_generation SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_chunks_document_generation_position
    ON chunks(document_id, processing_generation, chunk_index);

CREATE INDEX IF NOT EXISTS idx_chunks_pending_embedding
    ON chunks(document_id, processing_generation, chunk_index)
    WHERE embedded_at IS NULL;

CREATE TABLE IF NOT EXISTS document_embedding_jobs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    processing_generation UUID NOT NULL,
    batch_start INT NOT NULL,
    batch_end INT NOT NULL,
    total_chunks INT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    publish_attempts INT NOT NULL DEFAULT 0,
    locked_at TIMESTAMPTZ,
    redis_stream_id TEXT,
    published_at TIMESTAMPTZ,
    processed_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT document_embedding_jobs_batch_check CHECK (
        batch_start >= 0
        AND batch_end > batch_start
        AND total_chunks > 0
        AND batch_end <= total_chunks
    ),
    CONSTRAINT document_embedding_jobs_status_check CHECK (
        status IN ('pending', 'publishing', 'published', 'processed', 'failed', 'obsolete')
    ),
    CONSTRAINT document_embedding_jobs_attempts_check CHECK (publish_attempts >= 0),
    UNIQUE(document_id, processing_generation, batch_start)
);

CREATE INDEX IF NOT EXISTS idx_document_embedding_jobs_dispatch
    ON document_embedding_jobs(status, available_at, created_at)
    WHERE status IN ('pending', 'publishing');

CREATE INDEX IF NOT EXISTS idx_document_embedding_jobs_document
    ON document_embedding_jobs(document_id, processing_generation, batch_start);

CREATE TABLE IF NOT EXISTS automation_events (
    id UUID PRIMARY KEY,
    document_id UUID REFERENCES documents(id) ON DELETE SET NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    event_type VARCHAR(100) NOT NULL,
    payload JSONB NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    publish_attempts INT NOT NULL DEFAULT 0,
    locked_at TIMESTAMPTZ,
    redis_stream_id TEXT,
    published_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT automation_events_status_check CHECK (
        status IN ('pending', 'publishing', 'published')
    ),
    CONSTRAINT automation_events_attempts_check CHECK (publish_attempts >= 0)
);

CREATE INDEX IF NOT EXISTS idx_automation_events_dispatch
    ON automation_events(status, available_at, created_at)
    WHERE status IN ('pending', 'publishing');
