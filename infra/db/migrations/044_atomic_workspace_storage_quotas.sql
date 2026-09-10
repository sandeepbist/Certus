-- Enforce workspace document and retained-original limits before crossing the
-- PostgreSQL/object-store boundary. A durable upload intent owns exactly one
-- reservation, so retries are idempotent and concurrent uploads cannot
-- oversubscribe a workspace.

ALTER TABLE tenant_config
    DROP CONSTRAINT IF EXISTS tenant_config_document_quota_check;
ALTER TABLE tenant_config
    ADD CONSTRAINT tenant_config_document_quota_check CHECK (
        max_documents IS NULL OR max_documents >= 0
    );
ALTER TABLE tenant_config
    DROP CONSTRAINT IF EXISTS tenant_config_storage_quota_check;
ALTER TABLE tenant_config
    ADD CONSTRAINT tenant_config_storage_quota_check CHECK (
        max_storage_bytes IS NULL OR max_storage_bytes >= 0
    );

INSERT INTO tenant_config (organization_id)
SELECT tenant_id
FROM (
    SELECT tenant_id FROM documents
    UNION
    SELECT tenant_id FROM document_upload_intents
    UNION
    SELECT tenant_id FROM document_source_objects
) AS workspace
ON CONFLICT (organization_id) DO NOTHING;

ALTER TABLE document_upload_intents
    ADD COLUMN quota_document_count SMALLINT;
ALTER TABLE document_upload_intents
    ADD COLUMN quota_original_bytes BIGINT;
ALTER TABLE document_upload_intents
    ADD COLUMN quota_state VARCHAR(16);
ALTER TABLE document_upload_intents
    ADD COLUMN quota_reserved_at TIMESTAMPTZ;
ALTER TABLE document_upload_intents
    ADD COLUMN quota_released_reason TEXT;
ALTER TABLE document_upload_intents
    ADD COLUMN quota_released_at TIMESTAMPTZ;

UPDATE document_upload_intents
SET quota_document_count = CASE WHEN replace_document_id IS NULL THEN 1 ELSE 0 END,
    quota_original_bytes = byte_length,
    quota_state = CASE WHEN status = 'completed' THEN 'committed' ELSE 'reserved' END,
    quota_reserved_at = created_at;

ALTER TABLE document_upload_intents
    ALTER COLUMN quota_document_count SET NOT NULL,
    ALTER COLUMN quota_original_bytes SET NOT NULL,
    ALTER COLUMN quota_state SET NOT NULL,
    ALTER COLUMN quota_document_count SET DEFAULT 0,
    ALTER COLUMN quota_original_bytes SET DEFAULT 0,
    ALTER COLUMN quota_state SET DEFAULT 'reserved';

ALTER TABLE document_upload_intents
    DROP CONSTRAINT document_upload_intents_status_check;
ALTER TABLE document_upload_intents
    ADD CONSTRAINT document_upload_intents_status_check CHECK (
        status IN (
            'pending_object', 'object_stored', 'finalizing',
            'completed', 'error', 'abandoned'
        )
    );
ALTER TABLE document_upload_intents
    DROP CONSTRAINT document_upload_intents_object_state_check;
ALTER TABLE document_upload_intents
    ADD CONSTRAINT document_upload_intents_object_state_check CHECK (
        (
            status IN ('pending_object', 'abandoned')
            AND object_version_id IS NULL
            AND object_stored_at IS NULL
        )
        OR (
            status IN ('object_stored', 'finalizing', 'completed')
            AND object_version_id IS NOT NULL
            AND object_stored_at IS NOT NULL
        )
        OR status = 'error'
    );
ALTER TABLE document_upload_intents
    ADD CONSTRAINT document_upload_intents_quota_units_check CHECK (
        quota_document_count IN (0, 1)
        AND quota_original_bytes >= 0
        AND quota_original_bytes = byte_length
        AND (
            (replace_document_id IS NULL AND quota_document_count = 1)
            OR (replace_document_id IS NOT NULL AND quota_document_count = 0)
        )
    );
ALTER TABLE document_upload_intents
    ADD CONSTRAINT document_upload_intents_quota_state_check CHECK (
        (status = 'completed' AND quota_state = 'committed')
        OR (status = 'abandoned' AND quota_state = 'released')
        OR (
            status NOT IN ('completed', 'abandoned')
            AND quota_state = 'reserved'
        )
    );
ALTER TABLE document_upload_intents
    ADD CONSTRAINT document_upload_intents_quota_release_check CHECK (
        (
            quota_state = 'released'
            AND quota_released_reason IS NOT NULL
            AND quota_released_at IS NOT NULL
        )
        OR (
            quota_state <> 'released'
            AND quota_released_reason IS NULL
            AND quota_released_at IS NULL
        )
    );

CREATE TABLE workspace_storage_usage (
    tenant_id TEXT PRIMARY KEY,
    committed_documents BIGINT NOT NULL DEFAULT 0,
    committed_original_bytes BIGINT NOT NULL DEFAULT 0,
    reserved_documents BIGINT NOT NULL DEFAULT 0,
    reserved_original_bytes BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT workspace_storage_usage_nonnegative_check CHECK (
        committed_documents >= 0
        AND committed_original_bytes >= 0
        AND reserved_documents >= 0
        AND reserved_original_bytes >= 0
    )
);

INSERT INTO workspace_storage_usage (
    tenant_id, committed_documents, committed_original_bytes,
    reserved_documents, reserved_original_bytes
)
SELECT
    config.organization_id,
    COALESCE(document_usage.document_count, 0),
    COALESCE(object_usage.original_bytes, 0),
    COALESCE(intent_usage.document_count, 0),
    COALESCE(intent_usage.original_bytes, 0)
FROM tenant_config AS config
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS document_count
    FROM documents
    WHERE tenant_id = config.organization_id AND deleted_at IS NULL
) AS document_usage ON TRUE
LEFT JOIN LATERAL (
    SELECT COALESCE(SUM(byte_length), 0) AS original_bytes
    FROM document_source_objects
    WHERE tenant_id = config.organization_id
      AND storage_backend = 's3'
      AND status <> 'deleted'
) AS object_usage ON TRUE
LEFT JOIN LATERAL (
    SELECT
        COALESCE(SUM(quota_document_count), 0) AS document_count,
        COALESCE(SUM(quota_original_bytes), 0) AS original_bytes
    FROM document_upload_intents
    WHERE tenant_id = config.organization_id AND quota_state = 'reserved'
) AS intent_usage ON TRUE;

CREATE OR REPLACE FUNCTION reserve_workspace_upload_quota(p_intent_id UUID)
RETURNS TABLE (
    accepted BOOLEAN,
    exceeded_dimension TEXT,
    document_usage BIGINT,
    document_limit BIGINT,
    storage_usage BIGINT,
    storage_limit BIGINT
)
LANGUAGE plpgsql
AS $$
DECLARE
    upload document_upload_intents%ROWTYPE;
    usage workspace_storage_usage%ROWTYPE;
    configured tenant_config%ROWTYPE;
BEGIN
    SELECT * INTO upload
    FROM document_upload_intents
    WHERE id = p_intent_id
    FOR UPDATE;
    IF NOT FOUND OR upload.quota_state <> 'reserved' THEN
        RAISE EXCEPTION 'upload intent is not reservable';
    END IF;

    INSERT INTO tenant_config (organization_id)
    VALUES (upload.tenant_id)
    ON CONFLICT (organization_id) DO NOTHING;
    SELECT * INTO configured
    FROM tenant_config
    WHERE organization_id = upload.tenant_id
    FOR UPDATE;

    INSERT INTO workspace_storage_usage (tenant_id)
    VALUES (upload.tenant_id)
    ON CONFLICT (tenant_id) DO NOTHING;
    SELECT * INTO usage
    FROM workspace_storage_usage
    WHERE tenant_id = upload.tenant_id
    FOR UPDATE;

    IF upload.quota_reserved_at IS NOT NULL THEN
        accepted := TRUE;
        exceeded_dimension := NULL;
        document_usage := usage.committed_documents + usage.reserved_documents;
        document_limit := configured.max_documents;
        storage_usage := usage.committed_original_bytes + usage.reserved_original_bytes;
        storage_limit := configured.max_storage_bytes;
        RETURN NEXT;
        RETURN;
    END IF;

    document_usage := usage.committed_documents + usage.reserved_documents
        + upload.quota_document_count;
    document_limit := configured.max_documents;
    storage_usage := usage.committed_original_bytes + usage.reserved_original_bytes
        + upload.quota_original_bytes;
    storage_limit := configured.max_storage_bytes;

    IF upload.quota_document_count > 0
       AND configured.max_documents IS NOT NULL
       AND document_usage > configured.max_documents THEN
        accepted := FALSE;
        exceeded_dimension := 'documents';
        RETURN NEXT;
        RETURN;
    END IF;
    IF upload.quota_original_bytes > 0
       AND configured.max_storage_bytes IS NOT NULL
       AND storage_usage > configured.max_storage_bytes THEN
        accepted := FALSE;
        exceeded_dimension := 'storage';
        RETURN NEXT;
        RETURN;
    END IF;

    UPDATE workspace_storage_usage
    SET reserved_documents = reserved_documents + upload.quota_document_count,
        reserved_original_bytes = reserved_original_bytes + upload.quota_original_bytes,
        updated_at = NOW()
    WHERE tenant_id = upload.tenant_id;
    UPDATE document_upload_intents
    SET quota_reserved_at = NOW(), updated_at = NOW()
    WHERE id = upload.id AND quota_reserved_at IS NULL;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'upload quota reservation raced with another writer';
    END IF;
    accepted := TRUE;
    exceeded_dimension := NULL;
    RETURN NEXT;
END;
$$;

CREATE OR REPLACE FUNCTION complete_workspace_upload(
    p_intent_id UUID,
    p_response_payload JSONB
)
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
    upload document_upload_intents%ROWTYPE;
BEGIN
    SELECT * INTO upload
    FROM document_upload_intents
    WHERE id = p_intent_id
    FOR UPDATE;
    IF NOT FOUND OR upload.status <> 'finalizing' OR upload.quota_state <> 'reserved'
       OR upload.quota_reserved_at IS NULL THEN
        RAISE EXCEPTION 'upload intent is not ready for quota commit';
    END IF;
    IF p_response_payload IS NULL THEN
        RAISE EXCEPTION 'upload completion payload is required';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM document_source_objects
        WHERE id = upload.source_object_id
          AND tenant_id = upload.tenant_id
          AND byte_length = upload.quota_original_bytes
          AND storage_backend = 's3'
          AND status <> 'deleted'
    ) THEN
        RAISE EXCEPTION 'upload source object is not durably catalogued';
    END IF;
    IF upload.quota_document_count = 1 AND NOT EXISTS (
        SELECT 1 FROM documents
        WHERE id = upload.document_id
          AND tenant_id = upload.tenant_id
          AND deleted_at IS NULL
    ) THEN
        RAISE EXCEPTION 'upload document is not durably catalogued';
    END IF;

    PERFORM 1
    FROM workspace_storage_usage
    WHERE tenant_id = upload.tenant_id
    FOR UPDATE;
    UPDATE workspace_storage_usage
    SET reserved_documents = reserved_documents - upload.quota_document_count,
        reserved_original_bytes = reserved_original_bytes - upload.quota_original_bytes,
        committed_documents = committed_documents + upload.quota_document_count,
        committed_original_bytes = committed_original_bytes + upload.quota_original_bytes,
        updated_at = NOW()
    WHERE tenant_id = upload.tenant_id
      AND reserved_documents >= upload.quota_document_count
      AND reserved_original_bytes >= upload.quota_original_bytes;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'workspace quota reservation is missing';
    END IF;

    UPDATE document_upload_intents
    SET status = 'completed', quota_state = 'committed',
        response_payload = p_response_payload, completed_at = NOW(),
        last_error = NULL, locked_at = NULL, lock_owner = NULL,
        updated_at = NOW()
    WHERE id = p_intent_id;
END;
$$;

CREATE OR REPLACE FUNCTION abandon_exhausted_unstored_upload(p_max_attempts INT)
RETURNS UUID
LANGUAGE plpgsql
AS $$
DECLARE
    upload document_upload_intents%ROWTYPE;
BEGIN
    IF p_max_attempts < 1 THEN
        RAISE EXCEPTION 'maximum upload attempts must be positive';
    END IF;
    SELECT * INTO upload
    FROM document_upload_intents
    WHERE status = 'error'
      AND quota_state = 'reserved'
      AND quota_reserved_at IS NOT NULL
      AND object_version_id IS NULL
      AND attempt_count >= p_max_attempts
    ORDER BY updated_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT 1;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    PERFORM 1
    FROM workspace_storage_usage
    WHERE tenant_id = upload.tenant_id
    FOR UPDATE;
    UPDATE workspace_storage_usage
    SET reserved_documents = reserved_documents - upload.quota_document_count,
        reserved_original_bytes = reserved_original_bytes - upload.quota_original_bytes,
        updated_at = NOW()
    WHERE tenant_id = upload.tenant_id
      AND reserved_documents >= upload.quota_document_count
      AND reserved_original_bytes >= upload.quota_original_bytes;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'workspace quota reservation is missing';
    END IF;

    UPDATE document_upload_intents
    SET status = 'abandoned', quota_state = 'released',
        quota_released_reason = 'retry_limit_exhausted_before_object_storage',
        quota_released_at = NOW(), available_at = NOW(),
        locked_at = NULL, lock_owner = NULL, updated_at = NOW()
    WHERE id = upload.id;
    RETURN upload.id;
END;
$$;

CREATE OR REPLACE FUNCTION maintain_workspace_document_quota()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    usage workspace_storage_usage%ROWTYPE;
    configured tenant_config%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.deleted_at IS NULL THEN
            UPDATE workspace_storage_usage
            SET committed_documents = committed_documents - 1, updated_at = NOW()
            WHERE tenant_id = OLD.tenant_id AND committed_documents >= 1;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'workspace document quota ledger is inconsistent';
            END IF;
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL THEN
        UPDATE workspace_storage_usage
        SET committed_documents = committed_documents - 1, updated_at = NOW()
        WHERE tenant_id = NEW.tenant_id AND committed_documents >= 1;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'workspace document quota ledger is inconsistent';
        END IF;
    ELSIF OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS NULL THEN
        SELECT * INTO configured FROM tenant_config
        WHERE organization_id = NEW.tenant_id FOR UPDATE;
        SELECT * INTO usage FROM workspace_storage_usage
        WHERE tenant_id = NEW.tenant_id FOR UPDATE;
        IF configured.max_documents IS NOT NULL
           AND usage.committed_documents + usage.reserved_documents + 1
               > configured.max_documents THEN
            RAISE EXCEPTION 'workspace document quota prevents restoration';
        END IF;
        UPDATE workspace_storage_usage
        SET committed_documents = committed_documents + 1, updated_at = NOW()
        WHERE tenant_id = NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_maintain_workspace_document_quota
    AFTER UPDATE OF deleted_at ON documents
    FOR EACH ROW EXECUTE FUNCTION maintain_workspace_document_quota();

CREATE TRIGGER trg_maintain_workspace_document_quota_on_delete
    AFTER DELETE ON documents
    FOR EACH ROW EXECUTE FUNCTION maintain_workspace_document_quota();

CREATE OR REPLACE FUNCTION require_workspace_document_reservation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM document_upload_intents
        WHERE tenant_id = NEW.tenant_id
          AND user_id = NEW.user_id
          AND document_id = NEW.id
          AND quota_document_count = 1
          AND quota_state = 'reserved'
          AND quota_reserved_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'new documents require an active workspace quota reservation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_require_workspace_document_reservation
    BEFORE INSERT ON documents
    FOR EACH ROW EXECUTE FUNCTION require_workspace_document_reservation();

CREATE OR REPLACE FUNCTION maintain_workspace_original_quota()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status <> 'deleted' AND NEW.status = 'deleted'
       AND NEW.storage_backend = 's3' THEN
        UPDATE workspace_storage_usage
        SET committed_original_bytes = committed_original_bytes - NEW.byte_length,
            updated_at = NOW()
        WHERE tenant_id = NEW.tenant_id
          AND committed_original_bytes >= NEW.byte_length;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'workspace storage quota ledger is inconsistent';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_maintain_workspace_original_quota
    AFTER UPDATE OF status ON document_source_objects
    FOR EACH ROW EXECUTE FUNCTION maintain_workspace_original_quota();

CREATE OR REPLACE FUNCTION require_workspace_original_reservation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.storage_backend = 's3' AND NOT EXISTS (
        SELECT 1
        FROM document_upload_intents
        WHERE tenant_id = NEW.tenant_id
          AND user_id = NEW.user_id
          AND document_id = NEW.document_id
          AND document_version_id = NEW.document_version_id
          AND source_object_id = NEW.id
          AND quota_original_bytes = NEW.byte_length
          AND quota_state = 'reserved'
          AND quota_reserved_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'stored originals require an active workspace quota reservation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_require_workspace_original_reservation
    BEFORE INSERT ON document_source_objects
    FOR EACH ROW EXECUTE FUNCTION require_workspace_original_reservation();

CREATE OR REPLACE FUNCTION protect_document_upload_intent_identity()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status = 'completed' AND OLD IS DISTINCT FROM NEW THEN
        RAISE EXCEPTION 'completed document upload intents are immutable';
    END IF;

    IF OLD.id IS DISTINCT FROM NEW.id
       OR OLD.idempotency_key IS DISTINCT FROM NEW.idempotency_key
       OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.user_id IS DISTINCT FROM NEW.user_id
       OR OLD.document_id IS DISTINCT FROM NEW.document_id
       OR OLD.document_version_id IS DISTINCT FROM NEW.document_version_id
       OR OLD.source_object_id IS DISTINCT FROM NEW.source_object_id
       OR OLD.replace_document_id IS DISTINCT FROM NEW.replace_document_id
       OR OLD.bucket IS DISTINCT FROM NEW.bucket
       OR OLD.object_key IS DISTINCT FROM NEW.object_key
       OR OLD.original_filename IS DISTINCT FROM NEW.original_filename
       OR OLD.claimed_mime_type IS DISTINCT FROM NEW.claimed_mime_type
       OR OLD.byte_length IS DISTINCT FROM NEW.byte_length
       OR OLD.content_sha256 IS DISTINCT FROM NEW.content_sha256
       OR OLD.checksum_sha256_base64 IS DISTINCT FROM NEW.checksum_sha256_base64
       OR OLD.request_metadata IS DISTINCT FROM NEW.request_metadata
       OR OLD.quota_document_count IS DISTINCT FROM NEW.quota_document_count
       OR OLD.quota_original_bytes IS DISTINCT FROM NEW.quota_original_bytes
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'document upload intent identity and request fields are immutable';
    END IF;

    IF OLD.quota_reserved_at IS NOT NULL
       AND OLD.quota_reserved_at IS DISTINCT FROM NEW.quota_reserved_at THEN
        RAISE EXCEPTION 'upload quota reservation time is write-once';
    END IF;

    IF (OLD.object_version_id IS NOT NULL
        AND OLD.object_version_id IS DISTINCT FROM NEW.object_version_id)
       OR (OLD.etag IS NOT NULL AND OLD.etag IS DISTINCT FROM NEW.etag)
       OR (OLD.storage_class IS NOT NULL AND OLD.storage_class IS DISTINCT FROM NEW.storage_class)
       OR (OLD.server_side_encryption IS NOT NULL
           AND OLD.server_side_encryption IS DISTINCT FROM NEW.server_side_encryption)
       OR (OLD.kms_key_id IS NOT NULL AND OLD.kms_key_id IS DISTINCT FROM NEW.kms_key_id)
       OR (OLD.bucket_key_enabled IS NOT NULL
           AND OLD.bucket_key_enabled IS DISTINCT FROM NEW.bucket_key_enabled)
       OR (OLD.object_stored_at IS NOT NULL
           AND OLD.object_stored_at IS DISTINCT FROM NEW.object_stored_at) THEN
        RAISE EXCEPTION 'document upload intent object result fields are write-once';
    END IF;

    IF NEW.attempt_count < OLD.attempt_count THEN
        RAISE EXCEPTION 'document upload intent attempt count cannot move backwards';
    END IF;
    IF (OLD.quota_state = 'committed' AND NEW.quota_state <> 'committed')
       OR (OLD.quota_state = 'released' AND NEW.quota_state <> 'released')
       OR (OLD.quota_state = 'reserved' AND NEW.quota_state NOT IN ('reserved', 'committed', 'released')) THEN
        RAISE EXCEPTION 'invalid upload quota transition: % -> %', OLD.quota_state, NEW.quota_state;
    END IF;

    IF (OLD.status = 'pending_object'
        AND NEW.status NOT IN ('pending_object', 'object_stored', 'error'))
       OR (OLD.status = 'object_stored'
           AND NEW.status NOT IN ('object_stored', 'finalizing', 'error'))
       OR (OLD.status = 'finalizing'
           AND NEW.status NOT IN ('object_stored', 'finalizing', 'completed', 'error'))
       OR (OLD.status = 'error'
           AND NEW.status NOT IN ('pending_object', 'object_stored', 'finalizing', 'error', 'abandoned'))
       OR (OLD.status = 'abandoned' AND NEW.status <> 'abandoned') THEN
        RAISE EXCEPTION 'invalid document upload intent state transition: % -> %', OLD.status, NEW.status;
    END IF;

    RETURN NEW;
END;
$$;

CREATE INDEX idx_document_upload_intents_quota_recovery
    ON document_upload_intents(status, attempt_count, updated_at, id)
    WHERE status = 'error' AND quota_state = 'reserved';

COMMENT ON TABLE workspace_storage_usage IS
    'Authoritative workspace quota counters; upload reservations prevent concurrent oversubscription.';
COMMENT ON COLUMN workspace_storage_usage.committed_original_bytes IS
    'Bytes of retained, available-or-lifecycle-pending S3 originals; parsed and derived artifacts are excluded.';
