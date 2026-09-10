-- Freeze the matching webhook IDs when the product event commits. A webhook
-- created after an event must never receive that historical event; disabling
-- or deleting a snapshotted webhook is still honored by the delivery worker.

ALTER TABLE webhook_events
    ADD COLUMN IF NOT EXISTS target_webhook_ids UUID[] NOT NULL DEFAULT '{}';

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
    matching_webhook_ids UUID[];
BEGIN
    SELECT COALESCE(array_agg(id ORDER BY created_at, id), '{}'::uuid[])
    INTO matching_webhook_ids
    FROM webhooks
    WHERE organization_id = event_organization_id
      AND user_id = event_user_id
      AND is_enabled = true
      AND event_type_name = ANY(events);

    IF cardinality(matching_webhook_ids) = 0 THEN
        RETURN NULL;
    END IF;

    INSERT INTO webhook_events (
        id, user_id, organization_id, event_type, idempotency_key, payload,
        target_webhook_ids
    ) VALUES (
        event_id,
        event_user_id,
        event_organization_id,
        event_type_name,
        event_key,
        COALESCE(event_payload, '{}'::jsonb),
        matching_webhook_ids
    )
    ON CONFLICT (organization_id, user_id, idempotency_key) DO NOTHING;

    IF FOUND THEN
        RETURN event_id;
    END IF;
    RETURN NULL;
END;
$$;
