-- Realtime trace messages are invalidation signals; the authenticated REST
-- trace remains authoritative. Retain only a bounded event summary and status
-- metrics instead of duplicating full outputs, citations, and tool details in
-- both PostgreSQL outbox rows and Redis streams.

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
                jsonb_strip_nulls(jsonb_build_object(
                    'run_id', NEW.id,
                    'event_index', event_index,
                    'agent', LEFT(trace_event ->> 'agent', 100),
                    'action', LEFT(trace_event ->> 'action', 1_000),
                    'status', LEFT(trace_event ->> 'status', 50),
                    'timestamp', trace_event -> 'timestamp',
                    'token_count', COALESCE(NEW.total_tokens, 0)
                ))
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
            jsonb_strip_nulls(jsonb_build_object(
                'run_id', NEW.id,
                'status', NEW.status,
                'model_used', NEW.model_used,
                'eval_score', NEW.eval_score,
                'latency_ms', NEW.latency_ms,
                'prompt_tokens', COALESCE(NEW.prompt_tokens, 0),
                'completion_tokens', COALESCE(NEW.completion_tokens, 0),
                'total_tokens', COALESCE(NEW.total_tokens, 0),
                'error_message', LEFT(NEW.error_message, 1_000),
                'completed_at', NEW.completed_at
            ))
        );
    END IF;

    RETURN NEW;
END;
$$;
