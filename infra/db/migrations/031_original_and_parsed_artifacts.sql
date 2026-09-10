-- Preserve each accepted upload as an exact, private, version-addressed object
-- and move parser output into its own immutable derived artifact. PostgreSQL is
-- the authoritative workflow/catalog state; S3-compatible storage is the byte
-- authority. The upload-intent ledger closes the database/object-store gap.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE document_upload_intents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    idempotency_key VARCHAR(128) NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    document_id UUID NOT NULL,
    document_version_id UUID NOT NULL,
    source_object_id UUID NOT NULL,
    replace_document_id UUID,
    bucket VARCHAR(255) NOT NULL,
    object_key TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    claimed_mime_type VARCHAR(100) NOT NULL,
    byte_length BIGINT NOT NULL,
    content_sha256 VARCHAR(64) NOT NULL,
    checksum_sha256_base64 VARCHAR(44) NOT NULL,
    request_metadata JSONB NOT NULL DEFAULT '{}',
    status VARCHAR(20) NOT NULL DEFAULT 'pending_object',
    object_version_id TEXT,
    etag TEXT,
    storage_class VARCHAR(64),
    server_side_encryption VARCHAR(64),
    kms_key_id TEXT,
    bucket_key_enabled BOOLEAN,
    attempt_count INT NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    locked_at TIMESTAMPTZ,
    lock_owner UUID,
    last_error TEXT,
    response_payload JSONB,
    object_stored_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT document_upload_intents_size_check CHECK (byte_length >= 0),
    CONSTRAINT document_upload_intents_hash_check CHECK (
        content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT document_upload_intents_checksum_check CHECK (
        checksum_sha256_base64 ~ '^[A-Za-z0-9+/]{43}=$'
    ),
    CONSTRAINT document_upload_intents_status_check CHECK (
        status IN (
            'pending_object', 'object_stored', 'finalizing',
            'completed', 'error'
        )
    ),
    CONSTRAINT document_upload_intents_attempt_check CHECK (attempt_count >= 0),
    CONSTRAINT document_upload_intents_replace_check CHECK (
        replace_document_id IS NULL OR replace_document_id = document_id
    ),
    CONSTRAINT document_upload_intents_object_state_check CHECK (
        (
            status = 'pending_object'
            AND object_version_id IS NULL
            AND object_stored_at IS NULL
        )
        OR (
            status IN ('object_stored', 'finalizing', 'completed')
            AND object_version_id IS NOT NULL
            AND object_stored_at IS NOT NULL
        )
        OR status = 'error'
    ),
    CONSTRAINT document_upload_intents_completion_check CHECK (
        (status = 'completed' AND completed_at IS NOT NULL AND response_payload IS NOT NULL)
        OR (status <> 'completed' AND completed_at IS NULL)
    ),
    UNIQUE(tenant_id, user_id, idempotency_key),
    UNIQUE(document_version_id),
    UNIQUE(source_object_id),
    UNIQUE(bucket, object_key)
);

CREATE INDEX idx_document_upload_intents_recovery
    ON document_upload_intents(status, available_at, created_at)
    WHERE status IN ('pending_object', 'object_stored', 'finalizing');

CREATE INDEX idx_document_upload_intents_scope
    ON document_upload_intents(tenant_id, user_id, created_at DESC, id);

CREATE TABLE document_source_objects (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_version_id UUID NOT NULL,
    document_id UUID NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    storage_backend VARCHAR(20) NOT NULL,
    bucket VARCHAR(255),
    object_key TEXT,
    object_version_id TEXT,
    original_filename TEXT NOT NULL,
    claimed_mime_type VARCHAR(100) NOT NULL,
    detected_mime_type VARCHAR(100),
    byte_length BIGINT NOT NULL,
    content_sha256 VARCHAR(64) NOT NULL,
    checksum_sha256_base64 VARCHAR(44) NOT NULL,
    etag TEXT,
    storage_class VARCHAR(64),
    server_side_encryption VARCHAR(64),
    kms_key_id TEXT,
    bucket_key_enabled BOOLEAN,
    status VARCHAR(20) NOT NULL,
    unavailable_reason TEXT,
    last_error TEXT,
    stored_at TIMESTAMPTZ,
    last_verified_at TIMESTAMPTZ,
    delete_requested_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    retention_blocked_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT document_source_objects_version_scope_fk FOREIGN KEY (
        document_version_id, document_id, tenant_id, user_id
    ) REFERENCES document_versions(id, document_id, tenant_id, user_id)
      ON DELETE CASCADE,
    CONSTRAINT document_source_objects_backend_check CHECK (
        storage_backend IN ('s3', 'unavailable')
    ),
    CONSTRAINT document_source_objects_hash_check CHECK (
        content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT document_source_objects_checksum_check CHECK (
        checksum_sha256_base64 ~ '^[A-Za-z0-9+/]{43}=$'
    ),
    CONSTRAINT document_source_objects_size_check CHECK (byte_length >= 0),
    CONSTRAINT document_source_objects_status_check CHECK (
        status IN (
            'available', 'unavailable', 'missing',
            'delete_pending', 'deleted', 'error'
        )
    ),
    CONSTRAINT document_source_objects_availability_check CHECK (
        (
            storage_backend = 's3'
            AND bucket IS NOT NULL
            AND object_key IS NOT NULL
            AND object_version_id IS NOT NULL
            AND stored_at IS NOT NULL
            AND status IN ('available', 'missing', 'delete_pending', 'deleted', 'error')
            AND unavailable_reason IS NULL
        )
        OR (
            storage_backend = 'unavailable'
            AND bucket IS NULL
            AND object_key IS NULL
            AND object_version_id IS NULL
            AND stored_at IS NULL
            AND status = 'unavailable'
            AND unavailable_reason IS NOT NULL
        )
    ),
    CONSTRAINT document_source_objects_deleted_state_check CHECK (
        (status = 'deleted' AND deleted_at IS NOT NULL)
        OR (status <> 'deleted' AND deleted_at IS NULL)
    ),
    UNIQUE(document_version_id),
    UNIQUE(id, document_version_id, document_id, tenant_id, user_id)
);

CREATE UNIQUE INDEX idx_document_source_objects_locator_unique
    ON document_source_objects(storage_backend, bucket, object_key)
    WHERE storage_backend = 's3';

CREATE INDEX idx_document_source_objects_scope
    ON document_source_objects(tenant_id, user_id, created_at DESC, id);

CREATE INDEX idx_document_source_objects_lifecycle
    ON document_source_objects(status, delete_requested_at, created_at);

CREATE TABLE document_parsed_artifacts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_object_id UUID NOT NULL,
    document_version_id UUID NOT NULL,
    document_id UUID NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    content_text TEXT,
    content_sha256 VARCHAR(64),
    byte_length BIGINT,
    mime_type VARCHAR(100) NOT NULL DEFAULT 'text/plain; charset=utf-8',
    producer_profile JSONB NOT NULL DEFAULT '{}',
    status VARCHAR(20) NOT NULL,
    unavailable_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT document_parsed_artifacts_source_scope_fk FOREIGN KEY (
        source_object_id, document_version_id, document_id, tenant_id, user_id
    ) REFERENCES document_source_objects(
        id, document_version_id, document_id, tenant_id, user_id
    ) ON DELETE CASCADE,
    CONSTRAINT document_parsed_artifacts_status_check CHECK (
        status IN ('ready', 'unavailable')
    ),
    CONSTRAINT document_parsed_artifacts_hash_check CHECK (
        content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT document_parsed_artifacts_size_check CHECK (
        byte_length IS NULL OR byte_length >= 0
    ),
    CONSTRAINT document_parsed_artifacts_availability_check CHECK (
        (
            status = 'ready'
            AND content_text IS NOT NULL
            AND content_sha256 IS NOT NULL
            AND byte_length IS NOT NULL
            AND unavailable_reason IS NULL
            AND content_sha256 = encode(
                digest(convert_to(content_text, 'UTF8'), 'sha256'),
                'hex'
            )
            AND byte_length = octet_length(convert_to(content_text, 'UTF8'))
        )
        OR (
            status = 'unavailable'
            AND content_text IS NULL
            AND content_sha256 IS NULL
            AND byte_length IS NULL
            AND unavailable_reason IS NOT NULL
        )
    ),
    UNIQUE(document_version_id),
    UNIQUE(id, document_version_id, document_id, tenant_id, user_id)
);

CREATE INDEX idx_document_parsed_artifacts_scope
    ON document_parsed_artifacts(tenant_id, user_id, document_id, document_version_id);

-- Historical originals cannot be reconstructed. Backfill an explicit
-- unavailable source-object record while preserving every parsed byte.
INSERT INTO document_source_objects (
    id, document_version_id, document_id, tenant_id, user_id,
    storage_backend, original_filename, claimed_mime_type,
    byte_length, content_sha256, checksum_sha256_base64,
    status, unavailable_reason, created_at, updated_at
)
SELECT
    uuid_generate_v5(uuid_ns_url(), 'certus:source-object:' || version.id::text),
    version.id,
    version.document_id,
    version.tenant_id,
    version.user_id,
    'unavailable',
    COALESCE(NULLIF(version.title, ''), 'legacy-document-' || version.version_number::text),
    version.mime_type,
    COALESCE(version.file_size_bytes, 0),
    version.content_hash,
    encode(decode(version.content_hash, 'hex'), 'base64'),
    'unavailable',
    'Original bytes were not retained before migration 031.',
    COALESCE(version.created_at, version.recorded_at),
    COALESCE(version.updated_at, version.created_at, version.recorded_at)
FROM document_versions AS version
ON CONFLICT (document_version_id) DO NOTHING;

INSERT INTO document_parsed_artifacts (
    id, source_object_id, document_version_id, document_id, tenant_id,
    user_id, content_text, content_sha256, byte_length, producer_profile,
    status, unavailable_reason, created_at
)
SELECT
    uuid_generate_v5(uuid_ns_url(), 'certus:parsed-artifact:' || version.id::text),
    source.id,
    version.id,
    version.document_id,
    version.tenant_id,
    version.user_id,
    version.raw_text,
    CASE
        WHEN version.raw_text IS NULL THEN NULL
        ELSE encode(digest(convert_to(version.raw_text, 'UTF8'), 'sha256'), 'hex')
    END,
    CASE
        WHEN version.raw_text IS NULL THEN NULL
        ELSE octet_length(convert_to(version.raw_text, 'UTF8'))
    END,
    version.parser_profile,
    CASE WHEN version.raw_text IS NULL THEN 'unavailable' ELSE 'ready' END,
    CASE
        WHEN version.raw_text IS NULL
            THEN 'No parsed text existed when migration 031 created artifact lineage.'
        ELSE NULL
    END,
    COALESCE(version.created_at, version.recorded_at)
FROM document_versions AS version
JOIN document_source_objects AS source
  ON source.document_version_id = version.id
ON CONFLICT (document_version_id) DO NOTHING;

ALTER TABLE document_derivations
    ADD COLUMN input_parsed_artifact_id UUID,
    ADD CONSTRAINT document_derivations_parsed_input_scope_fk FOREIGN KEY (
        input_parsed_artifact_id, document_version_id, document_id, tenant_id, user_id
    ) REFERENCES document_parsed_artifacts(
        id, document_version_id, document_id, tenant_id, user_id
    ) ON DELETE CASCADE;

UPDATE document_derivations AS derivation
SET input_parsed_artifact_id = parsed.id
FROM document_parsed_artifacts AS parsed
WHERE parsed.document_version_id = derivation.document_version_id
  AND derivation.input_parsed_artifact_id IS NULL;

-- This is deliberately an expand migration. Existing processes may continue
-- writing the legacy raw_text projection during a rolling deployment. Runtime
-- code begins dual-writing the parsed artifact and a later contract migration
-- makes the lineage link mandatory before clearing the projections.

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
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'document upload intent identity and request fields are immutable';
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

    IF (OLD.status = 'pending_object'
        AND NEW.status NOT IN ('pending_object', 'object_stored', 'error'))
       OR (OLD.status = 'object_stored'
           AND NEW.status NOT IN ('object_stored', 'finalizing', 'error'))
       OR (OLD.status = 'finalizing'
           AND NEW.status NOT IN ('object_stored', 'finalizing', 'completed', 'error'))
       OR (OLD.status = 'error'
           AND NEW.status NOT IN ('pending_object', 'object_stored', 'finalizing', 'error')) THEN
        RAISE EXCEPTION 'invalid document upload intent state transition: % -> %', OLD.status, NEW.status;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_protect_document_upload_intent_identity
    BEFORE UPDATE ON document_upload_intents
    FOR EACH ROW EXECUTE FUNCTION protect_document_upload_intent_identity();

CREATE OR REPLACE FUNCTION protect_document_source_object()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.id IS DISTINCT FROM NEW.id
       OR OLD.document_version_id IS DISTINCT FROM NEW.document_version_id
       OR OLD.document_id IS DISTINCT FROM NEW.document_id
       OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.user_id IS DISTINCT FROM NEW.user_id
       OR OLD.storage_backend IS DISTINCT FROM NEW.storage_backend
       OR OLD.bucket IS DISTINCT FROM NEW.bucket
       OR OLD.object_key IS DISTINCT FROM NEW.object_key
       OR OLD.object_version_id IS DISTINCT FROM NEW.object_version_id
       OR OLD.original_filename IS DISTINCT FROM NEW.original_filename
       OR OLD.claimed_mime_type IS DISTINCT FROM NEW.claimed_mime_type
       OR OLD.detected_mime_type IS DISTINCT FROM NEW.detected_mime_type
       OR OLD.byte_length IS DISTINCT FROM NEW.byte_length
       OR OLD.content_sha256 IS DISTINCT FROM NEW.content_sha256
       OR OLD.checksum_sha256_base64 IS DISTINCT FROM NEW.checksum_sha256_base64
       OR OLD.etag IS DISTINCT FROM NEW.etag
       OR OLD.storage_class IS DISTINCT FROM NEW.storage_class
       OR OLD.server_side_encryption IS DISTINCT FROM NEW.server_side_encryption
       OR OLD.kms_key_id IS DISTINCT FROM NEW.kms_key_id
       OR OLD.bucket_key_enabled IS DISTINCT FROM NEW.bucket_key_enabled
       OR OLD.stored_at IS DISTINCT FROM NEW.stored_at
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'document source object identity and storage fields are immutable';
    END IF;

    IF OLD.last_verified_at IS NOT NULL
       AND (NEW.last_verified_at IS NULL OR NEW.last_verified_at < OLD.last_verified_at) THEN
        RAISE EXCEPTION 'document source object verification time cannot move backwards';
    END IF;

    IF (OLD.delete_requested_at IS NOT NULL
        AND OLD.delete_requested_at IS DISTINCT FROM NEW.delete_requested_at)
       OR (OLD.deleted_at IS NOT NULL AND OLD.deleted_at IS DISTINCT FROM NEW.deleted_at) THEN
        RAISE EXCEPTION 'document source object lifecycle timestamps are write-once';
    END IF;

    IF (OLD.status = 'unavailable' AND NEW.status <> 'unavailable')
       OR (OLD.status = 'available'
           AND NEW.status NOT IN ('available', 'missing', 'delete_pending', 'error'))
       OR (OLD.status = 'missing'
           AND NEW.status NOT IN ('available', 'missing', 'delete_pending', 'error'))
       OR (OLD.status = 'error'
           AND NEW.status NOT IN ('available', 'missing', 'delete_pending', 'error'))
       OR (OLD.status = 'delete_pending'
           AND NEW.status NOT IN ('available', 'delete_pending', 'deleted', 'error'))
       OR (OLD.status = 'deleted' AND NEW.status <> 'deleted') THEN
        RAISE EXCEPTION 'invalid document source object state transition: % -> %', OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_protect_document_source_object
    BEFORE UPDATE ON document_source_objects
    FOR EACH ROW EXECUTE FUNCTION protect_document_source_object();

CREATE OR REPLACE FUNCTION prevent_unerased_source_object_delete()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status NOT IN ('deleted', 'unavailable') THEN
        RAISE EXCEPTION 'source object must be verifiably deleted before catalog erasure';
    END IF;
    RETURN OLD;
END;
$$;

CREATE TRIGGER trg_prevent_unerased_source_object_delete
    BEFORE DELETE ON document_source_objects
    FOR EACH ROW EXECUTE FUNCTION prevent_unerased_source_object_delete();

CREATE OR REPLACE FUNCTION protect_document_parsed_artifact()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD IS DISTINCT FROM NEW THEN
        RAISE EXCEPTION 'document parsed artifact fields are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_protect_document_parsed_artifact
    BEFORE UPDATE ON document_parsed_artifacts
    FOR EACH ROW EXECUTE FUNCTION protect_document_parsed_artifact();

CREATE OR REPLACE FUNCTION protect_document_derivation_identity()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.id IS DISTINCT FROM NEW.id
       OR OLD.document_version_id IS DISTINCT FROM NEW.document_version_id
       OR OLD.document_id IS DISTINCT FROM NEW.document_id
       OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.user_id IS DISTINCT FROM NEW.user_id
       OR OLD.processing_generation IS DISTINCT FROM NEW.processing_generation
       OR OLD.artifact_type IS DISTINCT FROM NEW.artifact_type
       OR OLD.input_text_hash IS DISTINCT FROM NEW.input_text_hash
       OR OLD.input_parsed_artifact_id IS DISTINCT FROM NEW.input_parsed_artifact_id
       OR OLD.parser_profile IS DISTINCT FROM NEW.parser_profile
       OR OLD.chunker_profile IS DISTINCT FROM NEW.chunker_profile
       OR OLD.embedding_profile IS DISTINCT FROM NEW.embedding_profile
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'document derivation identity and producer fields are immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_protect_document_derivation_identity ON document_derivations;

CREATE TRIGGER trg_protect_document_derivation_identity
    BEFORE UPDATE ON document_derivations
    FOR EACH ROW EXECUTE FUNCTION protect_document_derivation_identity();

COMMENT ON TABLE document_upload_intents IS
    'Durable idempotent state bridging an authorized upload, create-once object write, and PostgreSQL finalization.';
COMMENT ON TABLE document_source_objects IS
    'One exact original object per immutable source version; physical locators and integrity provenance cannot be rewritten.';
COMMENT ON TABLE document_parsed_artifacts IS
    'Immutable parser output derived from one exact original object and consumed by chunk derivations.';
COMMENT ON COLUMN document_versions.raw_text IS
    'Legacy parsed-text projection retained during the 031 expand phase; document_parsed_artifacts is the new authority.';
COMMENT ON COLUMN documents.raw_text IS
    'Legacy current-version projection retained during the 031 expand phase and removed by a later contract migration.';
COMMENT ON COLUMN document_derivations.input_parsed_artifact_id IS
    'Exact immutable parsed artifact consumed by this derivation; nullable only during the 031 rollout window.';
