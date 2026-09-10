-- Add the control-plane and shadow relation required to re-embed a workspace
-- without replacing its currently searchable vectors in place. Runtime search
-- deliberately stays on the established chunks.embedding path until a later
-- migration integrates dual-write and active-generation serving.

CREATE TABLE workspace_embedding_corpus_revisions (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    revision BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id),
    CONSTRAINT workspace_embedding_corpus_scope_check CHECK (
        btrim(tenant_id) <> '' AND btrim(user_id) <> ''
    ),
    CONSTRAINT workspace_embedding_corpus_revision_check CHECK (revision >= 0)
);

CREATE TABLE workspace_embedding_generations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    embedding_profile VARCHAR(255) NOT NULL
        REFERENCES embedding_profiles(identifier)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    source_corpus_revision BIGINT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'building',
    expected_chunk_count INT NOT NULL,
    embedded_chunk_count INT NOT NULL DEFAULT 0,
    failed_chunk_count INT NOT NULL DEFAULT 0,
    evaluation_report JSONB NOT NULL DEFAULT '{}',
    previous_generation_id UUID,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sealed_at TIMESTAMPTZ,
    activated_at TIMESTAMPTZ,
    retired_at TIMESTAMPTZ,
    stale_at TIMESTAMPTZ,
    rollback_until TIMESTAMPTZ,
    retain_until TIMESTAMPTZ,
    CONSTRAINT workspace_embedding_generations_scope_check CHECK (
        btrim(tenant_id) <> '' AND btrim(user_id) <> ''
    ),
    CONSTRAINT workspace_embedding_generations_revision_check CHECK (
        source_corpus_revision >= 0
    ),
    CONSTRAINT workspace_embedding_generations_status_check CHECK (
        status IN (
            'building', 'ready', 'active', 'retired', 'stale',
            'failed', 'cancelled', 'rolled_back'
        )
    ),
    CONSTRAINT workspace_embedding_generations_counts_check CHECK (
        expected_chunk_count >= 0
        AND embedded_chunk_count >= 0
        AND failed_chunk_count >= 0
        AND embedded_chunk_count + failed_chunk_count <= expected_chunk_count
    ),
    CONSTRAINT workspace_embedding_generations_evaluation_check CHECK (
        jsonb_typeof(evaluation_report) = 'object'
    ),
    CONSTRAINT workspace_embedding_generations_rollback_check CHECK (
        rollback_until IS NULL OR activated_at IS NOT NULL
    ),
    UNIQUE(id, tenant_id, user_id)
);

ALTER TABLE workspace_embedding_generations
    ADD CONSTRAINT workspace_embedding_generations_previous_scope_fk
    FOREIGN KEY (previous_generation_id, tenant_id, user_id)
    REFERENCES workspace_embedding_generations(id, tenant_id, user_id)
    ON UPDATE RESTRICT ON DELETE RESTRICT;

CREATE UNIQUE INDEX idx_workspace_embedding_generation_active
    ON workspace_embedding_generations(tenant_id, user_id)
    WHERE status = 'active';

CREATE UNIQUE INDEX idx_workspace_embedding_generation_open_profile
    ON workspace_embedding_generations(tenant_id, user_id, embedding_profile)
    WHERE status IN ('building', 'ready');

CREATE INDEX idx_workspace_embedding_generation_history
    ON workspace_embedding_generations(tenant_id, user_id, created_at DESC, id);

CREATE UNIQUE INDEX idx_chunks_id_scope_unique
    ON chunks(id, tenant_id, user_id);

CREATE TABLE chunk_embedding_vectors (
    generation_id UUID NOT NULL,
    chunk_id UUID NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    content_sha256 VARCHAR(64) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    embedding vector(1536),
    provider_metadata JSONB NOT NULL DEFAULT '{}',
    attempt_count INT NOT NULL DEFAULT 0,
    last_error_code VARCHAR(80),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_attempt_at TIMESTAMPTZ,
    embedded_at TIMESTAMPTZ,
    PRIMARY KEY (generation_id, chunk_id),
    CONSTRAINT chunk_embedding_vectors_generation_scope_fk FOREIGN KEY (
        generation_id, tenant_id, user_id
    ) REFERENCES workspace_embedding_generations(id, tenant_id, user_id)
      ON UPDATE RESTRICT ON DELETE CASCADE,
    CONSTRAINT chunk_embedding_vectors_chunk_scope_fk FOREIGN KEY (
        chunk_id, tenant_id, user_id
    ) REFERENCES chunks(id, tenant_id, user_id)
      ON UPDATE RESTRICT ON DELETE CASCADE,
    CONSTRAINT chunk_embedding_vectors_hash_check CHECK (
        content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT chunk_embedding_vectors_status_check CHECK (
        status IN ('pending', 'embedded', 'failed')
    ),
    CONSTRAINT chunk_embedding_vectors_attempt_check CHECK (attempt_count >= 0),
    CONSTRAINT chunk_embedding_vectors_metadata_check CHECK (
        jsonb_typeof(provider_metadata) = 'object'
        AND octet_length(provider_metadata::TEXT) <= 16384
    ),
    CONSTRAINT chunk_embedding_vectors_state_check CHECK (
        (
            status = 'pending'
            AND embedding IS NULL
            AND embedded_at IS NULL
            AND last_error_code IS NULL
            AND last_error IS NULL
        ) OR (
            status = 'embedded'
            AND embedding IS NOT NULL
            AND embedded_at IS NOT NULL
            AND last_error_code IS NULL
            AND last_error IS NULL
        ) OR (
            status = 'failed'
            AND embedding IS NULL
            AND embedded_at IS NULL
            AND last_error_code IS NOT NULL
            AND last_error IS NOT NULL
        )
    )
);

CREATE INDEX idx_chunk_embedding_vectors_pending
    ON chunk_embedding_vectors(generation_id, status, chunk_id)
    WHERE status IN ('pending', 'failed');

CREATE INDEX idx_chunk_embedding_vectors_scope
    ON chunk_embedding_vectors(tenant_id, user_id, generation_id);

CREATE INDEX idx_chunk_embedding_vectors_hnsw
    ON chunk_embedding_vectors
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200)
    WHERE status = 'embedded';

CREATE FUNCTION protect_workspace_embedding_generation_identity()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.id IS DISTINCT FROM NEW.id
       OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.user_id IS DISTINCT FROM NEW.user_id
       OR OLD.embedding_profile IS DISTINCT FROM NEW.embedding_profile
       OR OLD.source_corpus_revision IS DISTINCT FROM NEW.source_corpus_revision
       OR OLD.expected_chunk_count IS DISTINCT FROM NEW.expected_chunk_count
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'embedding generation identity and snapshot fields are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_protect_workspace_embedding_generation_identity
    BEFORE UPDATE ON workspace_embedding_generations
    FOR EACH ROW EXECUTE FUNCTION protect_workspace_embedding_generation_identity();

CREATE FUNCTION protect_chunk_embedding_vector_identity()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.generation_id IS DISTINCT FROM NEW.generation_id
       OR OLD.chunk_id IS DISTINCT FROM NEW.chunk_id
       OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.user_id IS DISTINCT FROM NEW.user_id
       OR OLD.content_sha256 IS DISTINCT FROM NEW.content_sha256
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'chunk embedding snapshot identity is immutable';
    END IF;
    IF OLD.status = 'embedded' THEN
        RAISE EXCEPTION 'completed chunk embeddings are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_protect_chunk_embedding_vector_identity
    BEFORE UPDATE ON chunk_embedding_vectors
    FOR EACH ROW EXECUTE FUNCTION protect_chunk_embedding_vector_identity();

CREATE FUNCTION refresh_workspace_embedding_generation_counts(
    target_generation_id UUID
)
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
    embedded_count INT;
    failed_count INT;
BEGIN
    SELECT
        COUNT(*) FILTER (WHERE status = 'embedded')::INT,
        COUNT(*) FILTER (WHERE status = 'failed')::INT
    INTO embedded_count, failed_count
    FROM chunk_embedding_vectors
    WHERE generation_id = target_generation_id;

    UPDATE workspace_embedding_generations
    SET embedded_chunk_count = embedded_count,
        failed_chunk_count = failed_count,
        updated_at = NOW()
    WHERE id = target_generation_id;
END;
$$;

CREATE FUNCTION start_workspace_embedding_generation(
    target_tenant_id TEXT,
    target_user_id TEXT,
    target_embedding_profile VARCHAR(255)
)
RETURNS UUID
LANGUAGE plpgsql
AS $$
DECLARE
    generation_id UUID := uuid_generate_v4();
    corpus_revision BIGINT;
    expected_count INT;
    inserted_count INT;
BEGIN
    IF btrim(COALESCE(target_tenant_id, '')) = ''
       OR btrim(COALESCE(target_user_id, '')) = '' THEN
        RAISE EXCEPTION 'embedding generation requires tenant and user identity';
    END IF;

    PERFORM 1
    FROM embedding_profiles
    WHERE identifier = target_embedding_profile;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'embedding profile is not registered: %', target_embedding_profile;
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended(target_tenant_id || chr(31) || target_user_id, 0)
    );

    INSERT INTO workspace_embedding_corpus_revisions (tenant_id, user_id)
    VALUES (target_tenant_id, target_user_id)
    ON CONFLICT (tenant_id, user_id) DO NOTHING;

    SELECT revision INTO corpus_revision
    FROM workspace_embedding_corpus_revisions
    WHERE tenant_id = target_tenant_id AND user_id = target_user_id
    FOR UPDATE;

    SELECT COUNT(*)::INT INTO expected_count
    FROM chunks AS chunk
    JOIN documents AS document ON document.id = chunk.document_id
    JOIN document_versions AS version
      ON version.id = chunk.document_version_id
     AND version.document_id = chunk.document_id
    WHERE chunk.tenant_id = target_tenant_id
      AND chunk.user_id = target_user_id
      AND document.tenant_id = target_tenant_id
      AND document.user_id = target_user_id
      AND version.tenant_id = target_tenant_id
      AND version.user_id = target_user_id
      AND document.deleted_at IS NULL
      AND version.status = 'ready'
      AND chunk.derivation_id = version.current_derivation_id;

    INSERT INTO workspace_embedding_generations (
        id, tenant_id, user_id, embedding_profile, source_corpus_revision,
        expected_chunk_count
    ) VALUES (
        generation_id, target_tenant_id, target_user_id,
        target_embedding_profile, corpus_revision, expected_count
    );

    INSERT INTO chunk_embedding_vectors (
        generation_id, chunk_id, tenant_id, user_id, content_sha256
    )
    SELECT generation_id, chunk.id, chunk.tenant_id, chunk.user_id,
           encode(digest(convert_to(chunk.content, 'UTF8'), 'sha256'), 'hex')
    FROM chunks AS chunk
    JOIN documents AS document ON document.id = chunk.document_id
    JOIN document_versions AS version
      ON version.id = chunk.document_version_id
     AND version.document_id = chunk.document_id
    WHERE chunk.tenant_id = target_tenant_id
      AND chunk.user_id = target_user_id
      AND document.tenant_id = target_tenant_id
      AND document.user_id = target_user_id
      AND version.tenant_id = target_tenant_id
      AND version.user_id = target_user_id
      AND document.deleted_at IS NULL
      AND version.status = 'ready'
      AND chunk.derivation_id = version.current_derivation_id
    ORDER BY chunk.id;

    GET DIAGNOSTICS inserted_count = ROW_COUNT;
    IF inserted_count <> expected_count THEN
        RAISE EXCEPTION 'embedding snapshot changed while it was being captured';
    END IF;

    RETURN generation_id;
END;
$$;

CREATE FUNCTION record_chunk_embedding_vector(
    target_generation_id UUID,
    target_chunk_id UUID,
    target_embedding vector(1536),
    target_provider_metadata JSONB DEFAULT '{}'
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    generation workspace_embedding_generations%ROWTYPE;
BEGIN
    SELECT * INTO generation
    FROM workspace_embedding_generations
    WHERE id = target_generation_id
    FOR UPDATE;

    IF NOT FOUND OR generation.status <> 'building' THEN
        RETURN FALSE;
    END IF;
    IF target_embedding IS NULL THEN
        RAISE EXCEPTION 'chunk embedding cannot be null';
    END IF;
    IF jsonb_typeof(COALESCE(target_provider_metadata, '{}')) <> 'object'
       OR octet_length(COALESCE(target_provider_metadata, '{}')::TEXT) > 16384 THEN
        RAISE EXCEPTION 'provider metadata must be a bounded JSON object';
    END IF;

    UPDATE chunk_embedding_vectors
    SET status = 'embedded', embedding = target_embedding,
        provider_metadata = COALESCE(target_provider_metadata, '{}'),
        attempt_count = attempt_count + 1,
        last_error_code = NULL, last_error = NULL,
        last_attempt_at = NOW(), embedded_at = NOW(), updated_at = NOW()
    WHERE generation_id = target_generation_id
      AND chunk_id = target_chunk_id
      AND status IN ('pending', 'failed');

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;
    PERFORM refresh_workspace_embedding_generation_counts(target_generation_id);
    RETURN TRUE;
END;
$$;

CREATE FUNCTION record_chunk_embedding_failure(
    target_generation_id UUID,
    target_chunk_id UUID,
    target_error_code VARCHAR(80),
    target_error TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    generation_status VARCHAR(20);
BEGIN
    SELECT status INTO generation_status
    FROM workspace_embedding_generations
    WHERE id = target_generation_id
    FOR UPDATE;

    IF generation_status IS DISTINCT FROM 'building' THEN
        RETURN FALSE;
    END IF;
    IF btrim(COALESCE(target_error_code, '')) = ''
       OR btrim(COALESCE(target_error, '')) = '' THEN
        RAISE EXCEPTION 'embedding failure requires a code and message';
    END IF;

    UPDATE chunk_embedding_vectors
    SET status = 'failed', embedding = NULL, provider_metadata = '{}',
        attempt_count = attempt_count + 1,
        last_error_code = left(target_error_code, 80),
        last_error = left(target_error, 2000),
        last_attempt_at = NOW(), embedded_at = NULL, updated_at = NOW()
    WHERE generation_id = target_generation_id
      AND chunk_id = target_chunk_id
      AND status IN ('pending', 'failed');

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;
    PERFORM refresh_workspace_embedding_generation_counts(target_generation_id);
    RETURN TRUE;
END;
$$;

CREATE FUNCTION seal_workspace_embedding_generation(
    target_generation_id UUID,
    target_evaluation_report JSONB
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    generation workspace_embedding_generations%ROWTYPE;
    current_revision BIGINT;
BEGIN
    SELECT * INTO generation
    FROM workspace_embedding_generations
    WHERE id = target_generation_id
    FOR UPDATE;

    IF NOT FOUND OR generation.status <> 'building' THEN
        RETURN FALSE;
    END IF;

    SELECT revision INTO current_revision
    FROM workspace_embedding_corpus_revisions
    WHERE tenant_id = generation.tenant_id AND user_id = generation.user_id
    FOR UPDATE;

    IF current_revision IS DISTINCT FROM generation.source_corpus_revision THEN
        RETURN FALSE;
    END IF;

    PERFORM refresh_workspace_embedding_generation_counts(target_generation_id);
    SELECT * INTO generation
    FROM workspace_embedding_generations
    WHERE id = target_generation_id;

    IF generation.embedded_chunk_count <> generation.expected_chunk_count
       OR generation.failed_chunk_count <> 0 THEN
        RETURN FALSE;
    END IF;

    IF jsonb_typeof(COALESCE(target_evaluation_report, '{}')) <> 'object'
       OR COALESCE(target_evaluation_report->>'decision', '') <> 'approved'
       OR COALESCE((target_evaluation_report->>'gates_passed')::BOOLEAN, FALSE) IS NOT TRUE
       OR COALESCE(target_evaluation_report->>'baseline_fingerprint', '') = ''
       OR COALESCE(target_evaluation_report->>'candidate_fingerprint', '') = '' THEN
        RAISE EXCEPTION 'an approved, fingerprinted evaluation report is required';
    END IF;

    UPDATE workspace_embedding_generations
    SET status = 'ready', evaluation_report = target_evaluation_report,
        sealed_at = NOW(), updated_at = NOW(), last_error = NULL
    WHERE id = target_generation_id AND status = 'building';
    RETURN FOUND;
END;
$$;

CREATE FUNCTION activate_workspace_embedding_generation(
    target_generation_id UUID,
    rollback_window INTERVAL DEFAULT INTERVAL '7 days'
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    generation workspace_embedding_generations%ROWTYPE;
    current_revision BIGINT;
    previous_id UUID;
BEGIN
    SELECT * INTO generation
    FROM workspace_embedding_generations
    WHERE id = target_generation_id
    FOR UPDATE;

    IF NOT FOUND OR generation.status <> 'ready' THEN
        RETURN FALSE;
    END IF;
    IF rollback_window < INTERVAL '1 hour'
       OR rollback_window > INTERVAL '30 days' THEN
        RAISE EXCEPTION 'rollback window must be between 1 hour and 30 days';
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended(generation.tenant_id || chr(31) || generation.user_id, 0)
    );
    SELECT revision INTO current_revision
    FROM workspace_embedding_corpus_revisions
    WHERE tenant_id = generation.tenant_id AND user_id = generation.user_id
    FOR UPDATE;

    IF current_revision IS DISTINCT FROM generation.source_corpus_revision
       OR generation.embedded_chunk_count <> generation.expected_chunk_count
       OR generation.failed_chunk_count <> 0 THEN
        RETURN FALSE;
    END IF;

    SELECT id INTO previous_id
    FROM workspace_embedding_generations
    WHERE tenant_id = generation.tenant_id
      AND user_id = generation.user_id
      AND status = 'active'
    FOR UPDATE;

    IF previous_id IS NOT NULL THEN
        UPDATE workspace_embedding_generations
        SET status = 'retired', retired_at = NOW(),
            retain_until = NOW() + rollback_window,
            rollback_until = NULL, updated_at = NOW()
        WHERE id = previous_id;
    END IF;

    UPDATE workspace_embedding_generations
    SET status = 'active', previous_generation_id = previous_id,
        activated_at = NOW(), retired_at = NULL, stale_at = NULL,
        rollback_until = CASE
            WHEN previous_id IS NULL THEN NULL
            ELSE NOW() + rollback_window
        END,
        retain_until = NULL, updated_at = NOW()
    WHERE id = target_generation_id AND status = 'ready';
    RETURN FOUND;
END;
$$;

CREATE FUNCTION rollback_workspace_embedding_generation(
    target_active_generation_id UUID
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    active_generation workspace_embedding_generations%ROWTYPE;
    previous_generation workspace_embedding_generations%ROWTYPE;
    current_revision BIGINT;
BEGIN
    SELECT * INTO active_generation
    FROM workspace_embedding_generations
    WHERE id = target_active_generation_id
    FOR UPDATE;

    IF NOT FOUND
       OR active_generation.status <> 'active'
       OR active_generation.previous_generation_id IS NULL
       OR active_generation.rollback_until IS NULL
       OR active_generation.rollback_until <= NOW() THEN
        RETURN FALSE;
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            active_generation.tenant_id || chr(31) || active_generation.user_id,
            0
        )
    );

    SELECT * INTO previous_generation
    FROM workspace_embedding_generations
    WHERE id = active_generation.previous_generation_id
      AND tenant_id = active_generation.tenant_id
      AND user_id = active_generation.user_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    SELECT revision INTO current_revision
    FROM workspace_embedding_corpus_revisions
    WHERE tenant_id = active_generation.tenant_id
      AND user_id = active_generation.user_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    IF previous_generation.status <> 'retired'
       OR previous_generation.source_corpus_revision <> current_revision
       OR previous_generation.embedded_chunk_count
            <> previous_generation.expected_chunk_count
       OR previous_generation.failed_chunk_count <> 0 THEN
        RETURN FALSE;
    END IF;

    UPDATE workspace_embedding_generations
    SET status = 'rolled_back', retired_at = NOW(), rollback_until = NULL,
        retain_until = NULL, updated_at = NOW()
    WHERE id = active_generation.id AND status = 'active';

    UPDATE workspace_embedding_generations
    SET status = 'active', retired_at = NULL, retain_until = NULL,
        rollback_until = NULL, updated_at = NOW()
    WHERE id = previous_generation.id AND status = 'retired';
    RETURN FOUND;
END;
$$;

CREATE FUNCTION invalidate_workspace_embedding_scope(
    target_tenant_id TEXT,
    target_user_id TEXT
)
RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO workspace_embedding_corpus_revisions (
        tenant_id, user_id, revision, updated_at
    ) VALUES (
        target_tenant_id, target_user_id, 1, NOW()
    )
    ON CONFLICT (tenant_id, user_id) DO UPDATE
    SET revision = workspace_embedding_corpus_revisions.revision + 1,
        updated_at = NOW();

    UPDATE workspace_embedding_generations
    SET status = 'stale', stale_at = COALESCE(stale_at, NOW()),
        rollback_until = NULL, updated_at = NOW(),
        last_error = COALESCE(
            last_error,
            'The searchable workspace corpus changed after this snapshot was captured.'
        )
    WHERE tenant_id = target_tenant_id
      AND user_id = target_user_id
      AND status IN ('building', 'ready', 'active');
END;
$$;

CREATE FUNCTION invalidate_embedding_generations_for_inserted_chunks()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    scope RECORD;
BEGIN
    FOR scope IN SELECT DISTINCT tenant_id, user_id FROM inserted_chunks LOOP
        PERFORM invalidate_workspace_embedding_scope(scope.tenant_id, scope.user_id);
    END LOOP;
    RETURN NULL;
END;
$$;

CREATE FUNCTION invalidate_embedding_generations_for_deleted_chunks()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    scope RECORD;
BEGIN
    FOR scope IN SELECT DISTINCT tenant_id, user_id FROM deleted_chunks LOOP
        PERFORM invalidate_workspace_embedding_scope(scope.tenant_id, scope.user_id);
    END LOOP;
    RETURN NULL;
END;
$$;

CREATE TRIGGER trg_invalidate_embedding_generations_on_chunk_insert
    AFTER INSERT ON chunks
    REFERENCING NEW TABLE AS inserted_chunks
    FOR EACH STATEMENT
    EXECUTE FUNCTION invalidate_embedding_generations_for_inserted_chunks();

CREATE TRIGGER trg_invalidate_embedding_generations_on_chunk_delete
    AFTER DELETE ON chunks
    REFERENCING OLD TABLE AS deleted_chunks
    FOR EACH STATEMENT
    EXECUTE FUNCTION invalidate_embedding_generations_for_deleted_chunks();

CREATE FUNCTION invalidate_embedding_generation_on_version_change()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status IS DISTINCT FROM NEW.status
       OR OLD.current_derivation_id IS DISTINCT FROM NEW.current_derivation_id THEN
        PERFORM invalidate_workspace_embedding_scope(NEW.tenant_id, NEW.user_id);
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_invalidate_embedding_generation_on_version_change
    AFTER UPDATE OF status, current_derivation_id ON document_versions
    FOR EACH ROW EXECUTE FUNCTION invalidate_embedding_generation_on_version_change();

CREATE FUNCTION invalidate_embedding_generation_on_document_visibility()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.deleted_at IS DISTINCT FROM NEW.deleted_at THEN
        PERFORM invalidate_workspace_embedding_scope(NEW.tenant_id, NEW.user_id);
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_invalidate_embedding_generation_on_document_visibility
    AFTER UPDATE OF deleted_at ON documents
    FOR EACH ROW EXECUTE FUNCTION invalidate_embedding_generation_on_document_visibility();

COMMENT ON TABLE workspace_embedding_generations IS
    'Workspace-scoped, exact-corpus embedding snapshots. Not a serving authority until runtime dual-write and generation-aware retrieval are delivered.';
COMMENT ON TABLE chunk_embedding_vectors IS
    'Shadow vectors for one immutable generation snapshot; completed vectors cannot be rewritten in place.';
COMMENT ON COLUMN workspace_embedding_generations.source_corpus_revision IS
    'Revision fence captured while the exact eligible chunk membership is copied into chunk_embedding_vectors.';
COMMENT ON COLUMN workspace_embedding_generations.evaluation_report IS
    'Measured candidate-versus-baseline gate required before activation; activation does not itself make runtime search generation-aware.';
