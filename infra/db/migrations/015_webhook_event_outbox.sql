-- Transactional webhook event outbox. Event rows are created in the same
-- transaction as the product state change, then dispatched to Temporal by a
-- separate worker. This closes the commit/publish gap without making product
-- requests wait on customer-controlled endpoints.

CREATE TABLE IF NOT EXISTS webhook_events (
    id UUID PRIMARY KEY,
    user_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    idempotency_key VARCHAR(300) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    status VARCHAR(30) NOT NULL DEFAULT 'pending',
    workflow_id TEXT UNIQUE,
    workflow_run_id TEXT,
    dispatch_attempts INT NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    locked_at TIMESTAMPTZ,
    dispatched_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    result JSONB NOT NULL DEFAULT '{}',
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (organization_id, user_id, idempotency_key),
    CONSTRAINT webhook_events_status_check
        CHECK (status IN ('pending', 'dispatching', 'dispatched', 'completed', 'failed')),
    CONSTRAINT webhook_events_dispatch_attempts_check CHECK (dispatch_attempts >= 0)
);

CREATE INDEX IF NOT EXISTS idx_webhook_events_dispatch_queue
    ON webhook_events(available_at, created_at, id)
    WHERE status IN ('pending', 'dispatching');

CREATE INDEX IF NOT EXISTS idx_webhook_events_tenant_user_created
    ON webhook_events(organization_id, user_id, created_at DESC);

ALTER TABLE webhook_deliveries
    ADD COLUMN IF NOT EXISTS event_id UUID REFERENCES webhook_events(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS delivery_key UUID;

CREATE UNIQUE INDEX IF NOT EXISTS idx_webhook_delivery_event_attempt_unique
    ON webhook_deliveries(webhook_id, event_id, attempt_number)
    WHERE event_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_event
    ON webhook_deliveries(event_id, attempted_at DESC)
    WHERE event_id IS NOT NULL;

CREATE OR REPLACE FUNCTION enqueue_webhook_event(
    event_organization_id TEXT,
    event_user_id TEXT,
    event_type_name TEXT,
    event_key TEXT,
    event_payload JSONB
) RETURNS UUID
LANGUAGE plpgsql
AS $$
DECLARE
    event_id UUID := uuid_generate_v5(
        uuid_ns_url(),
        'certus:webhook:' || event_organization_id || ':' || event_user_id || ':' || event_key
    );
BEGIN
    INSERT INTO webhook_events (
        id, user_id, organization_id, event_type, idempotency_key, payload
    )
    SELECT
        event_id,
        event_user_id,
        event_organization_id,
        event_type_name,
        event_key,
        COALESCE(event_payload, '{}'::jsonb)
    WHERE EXISTS (
        SELECT 1
        FROM webhooks
        WHERE organization_id = event_organization_id
          AND user_id = event_user_id
          AND is_enabled = true
          AND event_type_name = ANY(events)
    )
    ON CONFLICT (organization_id, user_id, idempotency_key) DO NOTHING;

    IF FOUND THEN
        RETURN event_id;
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION capture_webhook_product_event()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_TABLE_NAME = 'documents' THEN
        IF NEW.status = 'ready' AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'ready') THEN
            PERFORM enqueue_webhook_event(
                NEW.tenant_id,
                NEW.user_id,
                'document_ready',
                'document_ready:' || NEW.id::text,
                jsonb_build_object(
                    'document_id', NEW.id,
                    'title', NEW.title,
                    'mime_type', NEW.mime_type,
                    'chunk_count', NEW.chunk_count,
                    'entity_count', NEW.entity_count,
                    'tags', NEW.tags
                )
            );
        END IF;
    ELSIF TG_TABLE_NAME = 'agent_runs' THEN
        IF NEW.status = 'completed' AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'completed') THEN
            PERFORM enqueue_webhook_event(
                NEW.tenant_id,
                NEW.user_id,
                'agent_run_completed',
                'agent_run_completed:' || NEW.id::text,
                jsonb_build_object(
                    'run_id', NEW.id,
                    'status', NEW.status,
                    'model', NEW.model_used,
                    'latency_ms', NEW.latency_ms,
                    'total_tokens', NEW.total_tokens,
                    'eval_score', NEW.eval_score
                )
            );
        END IF;
    ELSIF TG_TABLE_NAME = 'automation_executions' THEN
        IF NEW.status = 'running' AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'running') THEN
            PERFORM enqueue_webhook_event(
                NEW.tenant_id,
                NEW.user_id,
                'automation_triggered',
                'automation_triggered:' || NEW.id::text,
                jsonb_build_object(
                    'execution_id', NEW.id,
                    'rule_id', NEW.rule_id,
                    'trigger_event', NEW.trigger_event,
                    'source_document_id', NEW.source_document_id,
                    'status', NEW.status
                )
            );
        END IF;
        IF NEW.status = 'failed' AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'failed') THEN
            PERFORM enqueue_webhook_event(
                NEW.tenant_id,
                NEW.user_id,
                'automation_failed',
                'automation_failed:' || NEW.id::text,
                jsonb_build_object(
                    'execution_id', NEW.id,
                    'rule_id', NEW.rule_id,
                    'trigger_event', NEW.trigger_event,
                    'source_document_id', NEW.source_document_id,
                    'status', NEW.status,
                    'error', NEW.error_message
                )
            );
        END IF;
    ELSIF TG_TABLE_NAME = 'memories' THEN
        IF TG_OP = 'INSERT' AND NEW.is_active = true THEN
            PERFORM enqueue_webhook_event(
                NEW.tenant_id,
                NEW.user_id,
                'memory_extracted',
                'memory_extracted:' || NEW.id::text,
                jsonb_build_object(
                    'memory_id', NEW.id,
                    'category', NEW.category,
                    'confidence', NEW.confidence,
                    'source_run_id', NEW.source_run_id
                )
            );
        END IF;
    ELSIF TG_TABLE_NAME = 'tasks' THEN
        IF TG_OP = 'INSERT' THEN
            PERFORM enqueue_webhook_event(
                NEW.tenant_id,
                NEW.user_id,
                'task_created',
                'task_created:' || NEW.id::text,
                jsonb_build_object(
                    'task_id', NEW.id,
                    'title', NEW.title,
                    'status', NEW.status,
                    'priority', NEW.priority,
                    'due_date', NEW.due_date,
                    'tags', NEW.tags,
                    'source_document_id', NEW.source_document_id,
                    'source_agent_run_id', NEW.source_agent_run_id
                )
            );
        END IF;
        IF NEW.status = 'completed' AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'completed') THEN
            PERFORM enqueue_webhook_event(
                NEW.tenant_id,
                NEW.user_id,
                'task_completed',
                'task_completed:' || NEW.id::text,
                jsonb_build_object(
                    'task_id', NEW.id,
                    'title', NEW.title,
                    'status', NEW.status,
                    'priority', NEW.priority,
                    'completed_at', NEW.updated_at
                )
            );
        END IF;
    ELSIF TG_TABLE_NAME = 'data_exports' THEN
        IF NEW.status = 'ready' AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'ready') THEN
            PERFORM enqueue_webhook_event(
                NEW.organization_id,
                NEW.user_id,
                'export_ready',
                'export_ready:' || NEW.id::text,
                jsonb_build_object(
                    'export_id', NEW.id,
                    'status', NEW.status,
                    'format', NEW.format,
                    'file_name', NEW.file_name,
                    'size_bytes', NEW.size_bytes,
                    'expires_at', NEW.expires_at
                )
            );
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_documents_webhook_events ON documents;
CREATE TRIGGER trg_documents_webhook_events
    AFTER INSERT OR UPDATE OF status ON documents
    FOR EACH ROW EXECUTE FUNCTION capture_webhook_product_event();

DROP TRIGGER IF EXISTS trg_agent_runs_webhook_events ON agent_runs;
CREATE TRIGGER trg_agent_runs_webhook_events
    AFTER INSERT OR UPDATE OF status ON agent_runs
    FOR EACH ROW EXECUTE FUNCTION capture_webhook_product_event();

DROP TRIGGER IF EXISTS trg_automation_executions_webhook_events ON automation_executions;
CREATE TRIGGER trg_automation_executions_webhook_events
    AFTER INSERT OR UPDATE OF status ON automation_executions
    FOR EACH ROW EXECUTE FUNCTION capture_webhook_product_event();

DROP TRIGGER IF EXISTS trg_memories_webhook_events ON memories;
CREATE TRIGGER trg_memories_webhook_events
    AFTER INSERT ON memories
    FOR EACH ROW EXECUTE FUNCTION capture_webhook_product_event();

DROP TRIGGER IF EXISTS trg_tasks_webhook_events ON tasks;
CREATE TRIGGER trg_tasks_webhook_events
    AFTER INSERT OR UPDATE OF status ON tasks
    FOR EACH ROW EXECUTE FUNCTION capture_webhook_product_event();

DROP TRIGGER IF EXISTS trg_data_exports_webhook_events ON data_exports;
CREATE TRIGGER trg_data_exports_webhook_events
    AFTER INSERT OR UPDATE OF status ON data_exports
    FOR EACH ROW EXECUTE FUNCTION capture_webhook_product_event();
