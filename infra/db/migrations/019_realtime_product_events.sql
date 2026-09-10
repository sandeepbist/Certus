-- Durable product-state events for live trace and document views. These events
-- are independent of alert preferences: they synchronize an open product view
-- and commit in the same transaction as the source record.

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS processing_total_chunks INT NOT NULL DEFAULT 0;

UPDATE documents
SET processing_total_chunks = GREATEST(chunk_count, 0)
WHERE processing_total_chunks = 0 AND status = 'processing';

ALTER TABLE documents
    DROP CONSTRAINT IF EXISTS documents_processing_total_chunks_check;
ALTER TABLE documents
    ADD CONSTRAINT documents_processing_total_chunks_check
    CHECK (processing_total_chunks >= 0);

CREATE TABLE IF NOT EXISTS realtime_events (
    id UUID PRIMARY KEY,
    user_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    channel VARCHAR(200) NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    status VARCHAR(30) NOT NULL DEFAULT 'pending',
    publish_attempts INT NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    locked_at TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    redis_stream_id TEXT,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT realtime_events_type_check
        CHECK (event_type IN ('trace.event', 'trace.status', 'document.status')),
    CONSTRAINT realtime_events_status_check
        CHECK (status IN ('pending', 'publishing', 'published')),
    CONSTRAINT realtime_events_publish_attempts_check CHECK (publish_attempts >= 0)
);

CREATE INDEX IF NOT EXISTS idx_realtime_events_publish_queue
    ON realtime_events(available_at, created_at, id)
    WHERE status IN ('pending', 'publishing');

CREATE INDEX IF NOT EXISTS idx_realtime_events_tenant_user_created
    ON realtime_events(organization_id, user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_realtime_events_published_retention
    ON realtime_events(published_at, id)
    WHERE status = 'published';

CREATE OR REPLACE FUNCTION enqueue_realtime_event(
    event_organization_id TEXT,
    event_user_id TEXT,
    event_channel TEXT,
    event_type_name TEXT,
    event_deduplication_key TEXT,
    event_payload JSONB
) RETURNS UUID
LANGUAGE plpgsql
AS $$
DECLARE
    generated_event_id UUID := uuid_generate_v5(
        uuid_ns_url(),
        'certus:realtime:' || event_organization_id || ':'
            || event_user_id || ':' || event_deduplication_key
    );
    inserted_event_id UUID;
BEGIN
    INSERT INTO realtime_events (
        id, user_id, organization_id, channel, event_type, payload
    ) VALUES (
        generated_event_id,
        event_user_id,
        event_organization_id,
        event_channel,
        event_type_name,
        COALESCE(event_payload, '{}'::jsonb)
    )
    ON CONFLICT (id) DO NOTHING
    RETURNING id INTO inserted_event_id;

    RETURN inserted_event_id;
END;
$$;

CREATE OR REPLACE FUNCTION capture_agent_run_realtime_events()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    previous_event_count INT := 0;
    current_event_count INT := jsonb_array_length(COALESCE(NEW.events, '[]'::jsonb));
    event_index INT;
    trace_event JSONB;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        previous_event_count := jsonb_array_length(COALESCE(OLD.events, '[]'::jsonb));
    END IF;
    previous_event_count := LEAST(previous_event_count, current_event_count);

    IF current_event_count > previous_event_count THEN
        FOR event_index IN previous_event_count..(current_event_count - 1) LOOP
            trace_event := NEW.events -> event_index;
            PERFORM enqueue_realtime_event(
                NEW.tenant_id,
                NEW.user_id,
                'trace:' || NEW.id::text,
                'trace.event',
                'trace:' || NEW.id::text || ':event:' || event_index::text,
                jsonb_build_object(
                    'run_id', NEW.id,
                    'event_index', event_index,
                    'token_count', COALESCE(NEW.total_tokens, 0)
                ) || COALESCE(trace_event, '{}'::jsonb)
            );
        END LOOP;
    END IF;

    IF TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM NEW.status THEN
        PERFORM enqueue_realtime_event(
            NEW.tenant_id,
            NEW.user_id,
            'trace:' || NEW.id::text,
            'trace.status',
            'trace:' || NEW.id::text || ':status:' || NEW.status,
            jsonb_build_object(
                'run_id', NEW.id,
                'status', NEW.status,
                'model_used', NEW.model_used,
                'output_response', NEW.output_response,
                'citations', COALESCE(NEW.citations, '[]'::jsonb),
                'eval_score', NEW.eval_score,
                'latency_ms', NEW.latency_ms,
                'prompt_tokens', COALESCE(NEW.prompt_tokens, 0),
                'completion_tokens', COALESCE(NEW.completion_tokens, 0),
                'total_tokens', COALESCE(NEW.total_tokens, 0),
                'error_message', NEW.error_message,
                'completed_at', NEW.completed_at
            )
        );
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_agent_runs_realtime_events ON agent_runs;
CREATE TRIGGER trg_agent_runs_realtime_events
    AFTER INSERT OR UPDATE OF events, status ON agent_runs
    FOR EACH ROW EXECUTE FUNCTION capture_agent_run_realtime_events();

CREATE OR REPLACE FUNCTION capture_document_realtime_status()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    progress_percent INT;
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.status IS NOT DISTINCT FROM NEW.status
       AND OLD.chunk_count IS NOT DISTINCT FROM NEW.chunk_count
       AND OLD.processing_total_chunks IS NOT DISTINCT FROM NEW.processing_total_chunks THEN
        RETURN NEW;
    END IF;

    progress_percent := CASE
        WHEN NEW.status = 'ready' THEN 100
        WHEN NEW.processing_total_chunks > 0 THEN LEAST(
            99,
            FLOOR(NEW.chunk_count * 100.0 / NEW.processing_total_chunks)::INT
        )
        ELSE 0
    END;

    PERFORM enqueue_realtime_event(
        NEW.tenant_id,
        NEW.user_id,
        'document:' || NEW.id::text,
        'document.status',
        'document:' || NEW.id::text || ':status:' || NEW.status || ':'
            || NEW.chunk_count::text || ':' || NEW.processing_total_chunks::text,
        jsonb_build_object(
            'document_id', NEW.id,
            'status', NEW.status,
            'progress', progress_percent,
            'chunk_count', NEW.chunk_count,
            'total_chunks', NEW.processing_total_chunks,
            'entity_count', NEW.entity_count,
            'error_message', NEW.error_message,
            'updated_at', NEW.updated_at
        )
    );

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_documents_realtime_status ON documents;
CREATE TRIGGER trg_documents_realtime_status
    AFTER INSERT OR UPDATE OF status, chunk_count, processing_total_chunks ON documents
    FOR EACH ROW EXECUTE FUNCTION capture_document_realtime_status();
