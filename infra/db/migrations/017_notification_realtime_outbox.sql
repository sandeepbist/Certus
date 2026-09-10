-- Durable, preference-aware in-app notifications and their real-time outbox.
-- Product notifications and stream events commit in the same PostgreSQL
-- transaction; a dispatcher publishes outbox rows to Redis after commit.

CREATE TABLE IF NOT EXISTS notification_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    notification_id UUID NOT NULL,
    user_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    event_type VARCHAR(30) NOT NULL,
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
    CONSTRAINT notification_events_type_check
        CHECK (event_type IN ('created', 'read_state_changed', 'deleted')),
    CONSTRAINT notification_events_status_check
        CHECK (status IN ('pending', 'publishing', 'published')),
    CONSTRAINT notification_events_publish_attempts_check CHECK (publish_attempts >= 0)
);

CREATE INDEX IF NOT EXISTS idx_notification_events_publish_queue
    ON notification_events(available_at, created_at, id)
    WHERE status IN ('pending', 'publishing');

CREATE INDEX IF NOT EXISTS idx_notification_events_tenant_user_created
    ON notification_events(organization_id, user_id, created_at DESC);

CREATE OR REPLACE FUNCTION respect_in_app_notification_preference()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM user_preferences
        WHERE user_id = NEW.user_id
          AND notification_in_app = false
    ) THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_notifications_in_app_preference ON notifications;
CREATE TRIGGER trg_notifications_in_app_preference
    BEFORE INSERT ON notifications
    FOR EACH ROW EXECUTE FUNCTION respect_in_app_notification_preference();

CREATE OR REPLACE FUNCTION capture_notification_event()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    notification_record notifications%ROWTYPE;
    notification_event_type VARCHAR(30);
BEGIN
    IF TG_OP = 'INSERT' THEN
        notification_record := NEW;
        notification_event_type := 'created';
    ELSIF TG_OP = 'UPDATE' THEN
        IF OLD.is_read IS NOT DISTINCT FROM NEW.is_read THEN
            RETURN NEW;
        END IF;
        notification_record := NEW;
        notification_event_type := 'read_state_changed';
    ELSE
        notification_record := OLD;
        notification_event_type := 'deleted';
    END IF;

    INSERT INTO notification_events (
        notification_id,
        user_id,
        organization_id,
        event_type,
        payload
    ) VALUES (
        notification_record.id,
        notification_record.user_id,
        notification_record.organization_id,
        notification_event_type,
        jsonb_build_object(
            'id', notification_record.id,
            'type', notification_record.type,
            'title', notification_record.title,
            'body', notification_record.body,
            'metadata', notification_record.metadata,
            'action_url', notification_record.action_url,
            'is_read', notification_record.is_read,
            'created_at', notification_record.created_at
        )
    );

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_notifications_realtime_events ON notifications;
CREATE TRIGGER trg_notifications_realtime_events
    AFTER INSERT OR UPDATE OF is_read OR DELETE ON notifications
    FOR EACH ROW EXECUTE FUNCTION capture_notification_event();

CREATE OR REPLACE FUNCTION enqueue_in_app_notification(
    notification_organization_id TEXT,
    notification_user_id TEXT,
    notification_type_name TEXT,
    notification_key TEXT,
    notification_title TEXT,
    notification_body TEXT,
    notification_metadata JSONB,
    notification_action_url TEXT
) RETURNS UUID
LANGUAGE plpgsql
AS $$
DECLARE
    generated_notification_id UUID := uuid_generate_v5(
        uuid_ns_url(),
        'certus:notification:' || notification_organization_id || ':'
            || notification_user_id || ':' || notification_key
    );
    inserted_notification_id UUID;
BEGIN
    INSERT INTO notifications (
        id, user_id, organization_id, type, title, body,
        metadata, action_url, is_read, created_at
    ) VALUES (
        generated_notification_id,
        notification_user_id,
        notification_organization_id,
        notification_type_name,
        notification_title,
        notification_body,
        COALESCE(notification_metadata, '{}'::jsonb),
        notification_action_url,
        false,
        NOW()
    )
    ON CONFLICT (id) DO NOTHING
    RETURNING id INTO inserted_notification_id;

    RETURN inserted_notification_id;
END;
$$;

CREATE OR REPLACE FUNCTION capture_product_notification()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_TABLE_NAME = 'documents' THEN
        IF NEW.status = 'ready' AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'ready') THEN
            PERFORM enqueue_in_app_notification(
                NEW.tenant_id,
                NEW.user_id,
                'document_ready',
                'document_ready:' || NEW.id::text,
                'Document ready: ' || COALESCE(NEW.title, 'Untitled document'),
                'Processing and indexing completed successfully.',
                jsonb_build_object('document_id', NEW.id),
                '/documents/' || NEW.id::text
            );
        END IF;
    ELSIF TG_TABLE_NAME = 'agent_runs' THEN
        IF NEW.status = 'completed' AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'completed') THEN
            PERFORM enqueue_in_app_notification(
                NEW.tenant_id,
                NEW.user_id,
                'agent_run_completed',
                'agent_run_completed:' || NEW.id::text,
                'Agent run completed',
                LEFT(COALESCE(NEW.input_query, 'Your request finished successfully.'), 500),
                jsonb_build_object('run_id', NEW.id, 'model', NEW.model_used),
                '/traces/' || NEW.id::text
            );
        END IF;
    ELSIF TG_TABLE_NAME = 'automation_executions' THEN
        IF NEW.status = 'running' AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'running') THEN
            PERFORM enqueue_in_app_notification(
                NEW.tenant_id,
                NEW.user_id,
                'automation_triggered',
                'automation_triggered:' || NEW.id::text,
                'Automation started',
                'A matching automation rule started processing an event.',
                jsonb_build_object(
                    'execution_id', NEW.id,
                    'rule_id', NEW.rule_id,
                    'trigger_event', NEW.trigger_event
                ),
                '/automations'
            );
        END IF;
        IF NEW.status = 'failed' AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'failed') THEN
            PERFORM enqueue_in_app_notification(
                NEW.tenant_id,
                NEW.user_id,
                'automation_failed',
                'automation_failed:' || NEW.id::text,
                'Automation failed',
                LEFT(COALESCE(NEW.error_message, 'The automation could not be completed.'), 2_000),
                jsonb_build_object(
                    'execution_id', NEW.id,
                    'rule_id', NEW.rule_id,
                    'trigger_event', NEW.trigger_event
                ),
                '/automations'
            );
        END IF;
    ELSIF TG_TABLE_NAME = 'memories' THEN
        IF TG_OP = 'INSERT' AND NEW.is_active = true THEN
            PERFORM enqueue_in_app_notification(
                NEW.tenant_id,
                NEW.user_id,
                'memory_extracted',
                'memory_extracted:' || NEW.id::text,
                'New memory saved',
                LEFT(NEW.fact, 500),
                jsonb_build_object('memory_id', NEW.id, 'category', NEW.category),
                '/memory'
            );
        END IF;
    ELSIF TG_TABLE_NAME = 'data_exports' THEN
        IF NEW.status = 'ready' AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'ready') THEN
            PERFORM enqueue_in_app_notification(
                NEW.organization_id,
                NEW.user_id,
                'export_ready',
                'export_ready:' || NEW.id::text,
                'Data export ready',
                'Your workspace export is ready to download.',
                jsonb_build_object(
                    'export_id', NEW.id,
                    'file_name', NEW.file_name,
                    'expires_at', NEW.expires_at
                ),
                '/settings'
            );
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_documents_notifications ON documents;
CREATE TRIGGER trg_documents_notifications
    AFTER INSERT OR UPDATE OF status ON documents
    FOR EACH ROW EXECUTE FUNCTION capture_product_notification();

DROP TRIGGER IF EXISTS trg_agent_runs_notifications ON agent_runs;
CREATE TRIGGER trg_agent_runs_notifications
    AFTER INSERT OR UPDATE OF status ON agent_runs
    FOR EACH ROW EXECUTE FUNCTION capture_product_notification();

DROP TRIGGER IF EXISTS trg_automation_executions_notifications ON automation_executions;
CREATE TRIGGER trg_automation_executions_notifications
    AFTER INSERT OR UPDATE OF status ON automation_executions
    FOR EACH ROW EXECUTE FUNCTION capture_product_notification();

DROP TRIGGER IF EXISTS trg_memories_notifications ON memories;
CREATE TRIGGER trg_memories_notifications
    AFTER INSERT ON memories
    FOR EACH ROW EXECUTE FUNCTION capture_product_notification();

DROP TRIGGER IF EXISTS trg_data_exports_notifications ON data_exports;
CREATE TRIGGER trg_data_exports_notifications
    AFTER INSERT OR UPDATE OF status ON data_exports
    FOR EACH ROW EXECUTE FUNCTION capture_product_notification();
