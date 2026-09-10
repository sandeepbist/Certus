-- Preserve source history independently from processing history. A logical
-- document points at one current source version; re-chunking and re-embedding
-- create derivations of that version instead of rewriting or deleting evidence.

DO $$
DECLARE
    installed_version TEXT;
    version_parts TEXT[];
BEGIN
    SELECT extversion INTO installed_version
    FROM pg_extension
    WHERE extname = 'vector';

    version_parts := regexp_match(installed_version, '^(\d+)\.(\d+)');
    IF version_parts IS NULL
       OR version_parts[1]::INT < 0
       OR (
            version_parts[1]::INT = 0
            AND version_parts[2]::INT < 8
       ) THEN
        RAISE EXCEPTION 'pgvector 0.8.0 or newer is required; found %', installed_version;
    END IF;
END;
$$;

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS current_version_id UUID;

CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_identity_scope_unique
    ON documents(id, tenant_id, user_id);

CREATE TABLE IF NOT EXISTS document_versions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id UUID NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    version_number INT NOT NULL,
    title VARCHAR(500),
    source_type VARCHAR(50) NOT NULL,
    mime_type VARCHAR(100) NOT NULL,
    file_size_bytes BIGINT,
    content_hash VARCHAR(64) NOT NULL,
    raw_text TEXT,
    tags TEXT[] NOT NULL DEFAULT '{}',
    source_time TIMESTAMPTZ,
    source_time_origin VARCHAR(30) NOT NULL DEFAULT 'unspecified',
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status VARCHAR(20) NOT NULL DEFAULT 'processing',
    error_message TEXT,
    processing_generation UUID NOT NULL,
    current_derivation_id UUID,
    pending_derivation_id UUID,
    parser_profile JSONB NOT NULL DEFAULT '{}',
    source_metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT document_versions_number_check CHECK (version_number > 0),
    CONSTRAINT document_versions_size_check CHECK (
        file_size_bytes IS NULL OR file_size_bytes >= 0
    ),
    CONSTRAINT document_versions_hash_check CHECK (
        content_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT document_versions_source_time_origin_check CHECK (
        source_time_origin IN ('unspecified', 'user_provided')
    ),
    CONSTRAINT document_versions_source_time_pair_check CHECK (
        (source_time IS NULL AND source_time_origin = 'unspecified')
        OR (source_time IS NOT NULL AND source_time_origin = 'user_provided')
    ),
    CONSTRAINT document_versions_status_check CHECK (
        status IN ('processing', 'ready', 'error')
    ),
    CONSTRAINT document_versions_distinct_derivations_check CHECK (
        pending_derivation_id IS NULL
        OR pending_derivation_id <> current_derivation_id
    ),
    CONSTRAINT document_versions_document_scope_fk FOREIGN KEY (
        document_id, tenant_id, user_id
    ) REFERENCES documents(id, tenant_id, user_id) ON DELETE CASCADE,
    UNIQUE(document_id, version_number),
    UNIQUE(id, document_id, tenant_id, user_id)
);

INSERT INTO document_versions (
    id, document_id, tenant_id, user_id, version_number, title, source_type,
    mime_type, file_size_bytes, content_hash, raw_text, tags, source_time,
    source_time_origin, recorded_at, status, error_message,
    processing_generation, parser_profile, source_metadata, created_at, updated_at
)
SELECT
    uuid_generate_v5(
        uuid_ns_url(),
        'certus:document-version:' || document.id::text || ':1'
    ),
    document.id,
    document.tenant_id,
    document.user_id,
    1,
    document.title,
    document.source_type,
    document.mime_type,
    document.file_size_bytes,
    document.content_hash,
    document.raw_text,
    COALESCE(document.tags, '{}'),
    NULL,
    'unspecified',
    COALESCE(document.created_at, NOW()),
    CASE
        WHEN document.status IN ('processing', 'ready', 'error') THEN document.status
        WHEN document.chunk_count > 0 THEN 'ready'
        ELSE 'error'
    END,
    document.error_message,
    document.processing_generation,
    jsonb_build_object(
        'schema_version', 1,
        'implementation', 'legacy:unversioned',
        'source_format', document.source_type
    ),
    COALESCE(document.metadata, '{}'),
    COALESCE(document.created_at, NOW()),
    COALESCE(document.updated_at, document.created_at, NOW())
FROM documents AS document
ON CONFLICT (document_id, version_number) DO NOTHING;

CREATE TABLE IF NOT EXISTS document_derivations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_version_id UUID NOT NULL,
    document_id UUID NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    processing_generation UUID NOT NULL,
    artifact_type VARCHAR(30) NOT NULL DEFAULT 'chunk_set',
    input_text_hash VARCHAR(64),
    parser_profile JSONB NOT NULL DEFAULT '{}',
    chunker_profile JSONB NOT NULL DEFAULT '{}',
    embedding_profile VARCHAR(255) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'processing',
    chunk_count INT NOT NULL DEFAULT 0,
    processing_total_chunks INT NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    superseded_at TIMESTAMPTZ,
    CONSTRAINT document_derivations_version_scope_fk FOREIGN KEY (
        document_version_id, document_id, tenant_id, user_id
    ) REFERENCES document_versions(id, document_id, tenant_id, user_id)
      ON DELETE CASCADE,
    CONSTRAINT document_derivations_artifact_type_check CHECK (
        artifact_type = 'chunk_set'
    ),
    CONSTRAINT document_derivations_input_hash_check CHECK (
        input_text_hash IS NULL OR input_text_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT document_derivations_embedding_profile_check CHECK (
        embedding_profile ~ '^embedding-space:v1:(local|openai|legacy):[A-Za-z0-9._-]+:1536$'
    ),
    CONSTRAINT document_derivations_status_check CHECK (
        status IN ('processing', 'ready', 'error', 'superseded')
    ),
    CONSTRAINT document_derivations_counts_check CHECK (
        chunk_count >= 0
        AND processing_total_chunks >= 0
        AND chunk_count <= processing_total_chunks
    ),
    UNIQUE(document_version_id, processing_generation),
    UNIQUE(id, document_version_id, document_id, tenant_id, user_id)
);

WITH generations AS (
    SELECT id AS document_id, processing_generation
    FROM documents
    UNION
    SELECT document_id, processing_generation
    FROM chunks
    UNION
    SELECT document_id, processing_generation
    FROM document_embedding_jobs
), generation_state AS (
    SELECT
        generation.document_id,
        generation.processing_generation,
        document.tenant_id,
        document.user_id,
        document.processing_generation AS current_generation,
        document.status AS current_status,
        document.error_message AS current_error,
        document.processing_total_chunks AS current_total_chunks,
        document.created_at,
        document.updated_at,
        version.id AS document_version_id,
        version.parser_profile,
        COALESCE(document.metadata->>'chunk_strategy', 'legacy:unknown') AS chunk_strategy,
        (
            SELECT COUNT(*)::INT
            FROM chunks AS chunk
            WHERE chunk.document_id = generation.document_id
              AND chunk.processing_generation = generation.processing_generation
              AND chunk.embedded_at IS NOT NULL
        ) AS embedded_count,
        (
            SELECT COUNT(*)::INT
            FROM chunks AS chunk
            WHERE chunk.document_id = generation.document_id
              AND chunk.processing_generation = generation.processing_generation
        ) AS stored_count,
        (
            SELECT MAX(job.total_chunks)::INT
            FROM document_embedding_jobs AS job
            WHERE job.document_id = generation.document_id
              AND job.processing_generation = generation.processing_generation
        ) AS job_total_chunks,
        COALESCE(
            (
                SELECT MIN(chunk.embedding_profile)
                FROM chunks AS chunk
                WHERE chunk.document_id = generation.document_id
                  AND chunk.processing_generation = generation.processing_generation
            ),
            (
                SELECT MIN(job.embedding_profile)
                FROM document_embedding_jobs AS job
                WHERE job.document_id = generation.document_id
                  AND job.processing_generation = generation.processing_generation
            ),
            'embedding-space:v1:legacy:unversioned:1536'
        ) AS embedding_profile
    FROM generations AS generation
    JOIN documents AS document ON document.id = generation.document_id
    JOIN document_versions AS version
      ON version.document_id = generation.document_id
     AND version.version_number = 1
)
INSERT INTO document_derivations (
    id, document_version_id, document_id, tenant_id, user_id,
    processing_generation, input_text_hash, parser_profile, chunker_profile,
    embedding_profile, status, chunk_count, processing_total_chunks,
    error_message, created_at, updated_at, completed_at, superseded_at
)
SELECT
    uuid_generate_v5(
        uuid_ns_url(),
        'certus:document-derivation:' || state.document_id::text || ':'
            || state.processing_generation::text
    ),
    state.document_version_id,
    state.document_id,
    state.tenant_id,
    state.user_id,
    state.processing_generation,
    NULL,
    state.parser_profile,
    jsonb_build_object(
        'schema_version', 1,
        'strategy', state.chunk_strategy,
        'implementation', 'legacy:unversioned'
    ),
    state.embedding_profile,
    CASE
        WHEN state.processing_generation <> state.current_generation THEN 'superseded'
        WHEN state.current_status = 'ready' THEN 'ready'
        WHEN state.current_status = 'error' THEN 'error'
        ELSE 'processing'
    END,
    LEAST(
        state.embedded_count,
        GREATEST(
            state.stored_count,
            COALESCE(state.job_total_chunks, 0),
            CASE
                WHEN state.processing_generation = state.current_generation
                    THEN state.current_total_chunks
                ELSE 0
            END
        )
    ),
    GREATEST(
        state.stored_count,
        COALESCE(state.job_total_chunks, 0),
        CASE
            WHEN state.processing_generation = state.current_generation
                THEN state.current_total_chunks
            ELSE 0
        END
    ),
    CASE
        WHEN state.processing_generation = state.current_generation
            THEN state.current_error
        ELSE NULL
    END,
    COALESCE(state.created_at, NOW()),
    COALESCE(state.updated_at, state.created_at, NOW()),
    CASE
        WHEN state.processing_generation = state.current_generation
         AND state.current_status = 'ready'
            THEN COALESCE(state.updated_at, NOW())
        ELSE NULL
    END,
    CASE
        WHEN state.processing_generation <> state.current_generation
            THEN COALESCE(state.updated_at, NOW())
        ELSE NULL
    END
FROM generation_state AS state
ON CONFLICT (document_version_id, processing_generation) DO NOTHING;

UPDATE document_versions AS version
SET current_derivation_id = derivation.id
FROM documents AS document
JOIN document_derivations AS derivation
  ON derivation.document_id = document.id
 AND derivation.processing_generation = document.processing_generation
WHERE version.document_id = document.id
  AND version.version_number = 1
  AND version.current_derivation_id IS NULL;

UPDATE documents AS document
SET current_version_id = version.id
FROM document_versions AS version
WHERE version.document_id = document.id
  AND version.version_number = 1
  AND document.current_version_id IS NULL;

ALTER TABLE documents
    ALTER COLUMN current_version_id SET NOT NULL,
    ADD CONSTRAINT documents_current_version_fk FOREIGN KEY (
        current_version_id, id, tenant_id, user_id
    ) REFERENCES document_versions(id, document_id, tenant_id, user_id)
      DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE document_versions
    ALTER COLUMN current_derivation_id SET NOT NULL,
    ADD CONSTRAINT document_versions_current_derivation_fk FOREIGN KEY (
        current_derivation_id, id, document_id, tenant_id, user_id
    ) REFERENCES document_derivations(
        id, document_version_id, document_id, tenant_id, user_id
    )
      DEFERRABLE INITIALLY DEFERRED,
    ADD CONSTRAINT document_versions_pending_derivation_fk FOREIGN KEY (
        pending_derivation_id, id, document_id, tenant_id, user_id
    ) REFERENCES document_derivations(
        id, document_version_id, document_id, tenant_id, user_id
    )
      DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS document_version_id UUID,
    ADD COLUMN IF NOT EXISTS derivation_id UUID;

UPDATE chunks AS chunk
SET document_version_id = derivation.document_version_id,
    derivation_id = derivation.id
FROM document_derivations AS derivation
WHERE derivation.document_id = chunk.document_id
  AND derivation.processing_generation = chunk.processing_generation
  AND (chunk.document_version_id IS NULL OR chunk.derivation_id IS NULL);

ALTER TABLE chunks
    ALTER COLUMN document_version_id SET NOT NULL,
    ALTER COLUMN derivation_id SET NOT NULL,
    ADD CONSTRAINT chunks_derivation_scope_fk FOREIGN KEY (
        derivation_id, document_version_id, document_id, tenant_id, user_id
    ) REFERENCES document_derivations(
        id, document_version_id, document_id, tenant_id, user_id
    )
      ON DELETE CASCADE;

DROP INDEX IF EXISTS idx_chunks_document_position_unique;

CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_derivation_position_unique
    ON chunks(derivation_id, chunk_index);

CREATE INDEX IF NOT EXISTS idx_chunks_version_derivation_position
    ON chunks(document_version_id, derivation_id, chunk_index);

ALTER TABLE document_embedding_jobs
    ADD COLUMN IF NOT EXISTS document_version_id UUID,
    ADD COLUMN IF NOT EXISTS derivation_id UUID;

UPDATE document_embedding_jobs AS job
SET document_version_id = derivation.document_version_id,
    derivation_id = derivation.id
FROM document_derivations AS derivation
WHERE derivation.document_id = job.document_id
  AND derivation.processing_generation = job.processing_generation
  AND (job.document_version_id IS NULL OR job.derivation_id IS NULL);

ALTER TABLE document_embedding_jobs
    ALTER COLUMN document_version_id SET NOT NULL,
    ALTER COLUMN derivation_id SET NOT NULL,
    ADD CONSTRAINT document_embedding_jobs_derivation_scope_fk FOREIGN KEY (
        derivation_id, document_version_id, document_id, tenant_id, user_id
    ) REFERENCES document_derivations(
        id, document_version_id, document_id, tenant_id, user_id
    )
      ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_document_versions_history
    ON document_versions(document_id, version_number DESC);

CREATE INDEX IF NOT EXISTS idx_document_versions_scope_time
    ON document_versions(tenant_id, user_id, recorded_at DESC, id);

CREATE INDEX IF NOT EXISTS idx_document_derivations_version_history
    ON document_derivations(document_version_id, created_at DESC, id);

CREATE INDEX IF NOT EXISTS idx_document_embedding_jobs_derivation
    ON document_embedding_jobs(derivation_id, batch_start);

CREATE OR REPLACE FUNCTION protect_document_version_source()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.id IS DISTINCT FROM NEW.id
       OR OLD.document_id IS DISTINCT FROM NEW.document_id
       OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.user_id IS DISTINCT FROM NEW.user_id
       OR OLD.version_number IS DISTINCT FROM NEW.version_number
       OR OLD.title IS DISTINCT FROM NEW.title
       OR OLD.source_type IS DISTINCT FROM NEW.source_type
       OR OLD.mime_type IS DISTINCT FROM NEW.mime_type
       OR OLD.file_size_bytes IS DISTINCT FROM NEW.file_size_bytes
       OR OLD.content_hash IS DISTINCT FROM NEW.content_hash
       OR OLD.raw_text IS DISTINCT FROM NEW.raw_text
       OR OLD.tags IS DISTINCT FROM NEW.tags
       OR OLD.source_time IS DISTINCT FROM NEW.source_time
       OR OLD.source_time_origin IS DISTINCT FROM NEW.source_time_origin
       OR OLD.recorded_at IS DISTINCT FROM NEW.recorded_at
       OR OLD.parser_profile IS DISTINCT FROM NEW.parser_profile
       OR OLD.source_metadata IS DISTINCT FROM NEW.source_metadata
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'document version source fields are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_protect_document_version_source
    BEFORE UPDATE ON document_versions
    FOR EACH ROW EXECUTE FUNCTION protect_document_version_source();

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
       OR OLD.parser_profile IS DISTINCT FROM NEW.parser_profile
       OR OLD.chunker_profile IS DISTINCT FROM NEW.chunker_profile
       OR OLD.embedding_profile IS DISTINCT FROM NEW.embedding_profile
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'document derivation identity and producer fields are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_protect_document_derivation_identity
    BEFORE UPDATE ON document_derivations
    FOR EACH ROW EXECUTE FUNCTION protect_document_derivation_identity();

CREATE OR REPLACE FUNCTION protect_chunk_provenance()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.id IS DISTINCT FROM NEW.id
       OR OLD.document_id IS DISTINCT FROM NEW.document_id
       OR OLD.document_version_id IS DISTINCT FROM NEW.document_version_id
       OR OLD.derivation_id IS DISTINCT FROM NEW.derivation_id
       OR OLD.user_id IS DISTINCT FROM NEW.user_id
       OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.content IS DISTINCT FROM NEW.content
       OR OLD.contextualized_content IS DISTINCT FROM NEW.contextualized_content
       OR OLD.chunk_index IS DISTINCT FROM NEW.chunk_index
       OR OLD.token_count IS DISTINCT FROM NEW.token_count
       OR OLD.start_char IS DISTINCT FROM NEW.start_char
       OR OLD.end_char IS DISTINCT FROM NEW.end_char
       OR OLD.page_number IS DISTINCT FROM NEW.page_number
       OR OLD.section_title IS DISTINCT FROM NEW.section_title
       OR OLD.language IS DISTINCT FROM NEW.language
       OR OLD.processing_generation IS DISTINCT FROM NEW.processing_generation
       OR OLD.embedding_profile IS DISTINCT FROM NEW.embedding_profile
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'chunk source and provenance fields are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_protect_chunk_provenance
    BEFORE UPDATE ON chunks
    FOR EACH ROW EXECUTE FUNCTION protect_chunk_provenance();

-- Ready events are version events. Using only the logical document ID would
-- suppress every ready webhook after version 1 because the outbox key is unique.
DROP TRIGGER IF EXISTS trg_documents_webhook_events ON documents;

CREATE OR REPLACE FUNCTION capture_document_version_webhook_event()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    logical_document documents%ROWTYPE;
BEGIN
    IF NEW.status = 'ready'
       AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'ready') THEN
        SELECT * INTO logical_document
        FROM documents
        WHERE id = NEW.document_id;

        PERFORM enqueue_webhook_event(
            NEW.tenant_id,
            NEW.user_id,
            'document_ready',
            'document_ready:' || NEW.document_id::text || ':' || NEW.id::text,
            jsonb_build_object(
                'document_id', NEW.document_id,
                'document_version_id', NEW.id,
                'version_number', NEW.version_number,
                'title', NEW.title,
                'mime_type', NEW.mime_type,
                'chunk_count', COALESCE((
                    SELECT derivation.chunk_count
                    FROM document_derivations AS derivation
                    WHERE derivation.id = NEW.current_derivation_id
                ), 0),
                'entity_count', COALESCE(logical_document.entity_count, 0),
                'tags', NEW.tags,
                'content_hash', NEW.content_hash,
                'source_time', NEW.source_time,
                'recorded_at', NEW.recorded_at
            )
        );
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_document_versions_webhook_events
    AFTER INSERT OR UPDATE OF status ON document_versions
    FOR EACH ROW EXECUTE FUNCTION capture_document_version_webhook_event();

CREATE OR REPLACE FUNCTION capture_document_realtime_status()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    progress_percent INT;
    current_version_number INT;
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.status IS NOT DISTINCT FROM NEW.status
       AND OLD.chunk_count IS NOT DISTINCT FROM NEW.chunk_count
       AND OLD.processing_total_chunks IS NOT DISTINCT FROM NEW.processing_total_chunks
       AND OLD.processing_generation IS NOT DISTINCT FROM NEW.processing_generation
       AND OLD.current_version_id IS NOT DISTINCT FROM NEW.current_version_id THEN
        RETURN NEW;
    END IF;

    SELECT version_number INTO current_version_number
    FROM document_versions
    WHERE id = NEW.current_version_id;

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
            'document_version_id', NEW.current_version_id,
            'version_number', current_version_number,
            'processing_generation', NEW.processing_generation,
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

COMMENT ON TABLE document_versions IS
    'Source-history records. Source identity and extracted text are immutable; processing state may advance.';
COMMENT ON TABLE document_derivations IS
    'Versioned chunk/embedding derivations. Reprocessing appends a derivation instead of deleting prior evidence.';
COMMENT ON COLUMN document_versions.pending_derivation_id IS
    'Shadow derivation being built; current_derivation_id stays searchable until an atomic ready swap.';
COMMENT ON COLUMN documents.current_version_id IS
    'Current source version for document management; retained ready versions remain independently searchable.';
