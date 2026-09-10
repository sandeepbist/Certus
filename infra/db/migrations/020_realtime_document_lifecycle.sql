-- A document can be reprocessed with the same status and chunk totals. Include
-- the source update timestamp in the deterministic outbox key so each distinct
-- processing lifecycle is delivered while publisher retries remain idempotent.

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
        'document:' || NEW.id::text || ':updated:' || NEW.updated_at::text || ':'
            || NEW.status || ':' || NEW.chunk_count::text || ':'
            || NEW.processing_total_chunks::text,
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
