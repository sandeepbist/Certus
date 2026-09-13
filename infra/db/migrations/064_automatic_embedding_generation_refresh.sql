-- Keep active workspace generations synchronized with corpus revisions without
-- repeatedly embedding unchanged history. Only system-authored, same-profile
-- refreshes may cut over automatically; operator/profile migrations retain the
-- explicit activation path.

ALTER TABLE workspace_embedding_generations
    ADD COLUMN creation_reason VARCHAR(30) NOT NULL DEFAULT 'operator',
    ADD CONSTRAINT workspace_embedding_generations_creation_reason_check CHECK (
        creation_reason IN ('operator', 'corpus_refresh')
    );

CREATE INDEX idx_workspace_embedding_generation_refresh_source
    ON workspace_embedding_generations(
        embedding_profile, updated_at, id
    )
    INCLUDE (tenant_id, user_id, source_corpus_revision)
    WHERE status = 'active';

CREATE INDEX idx_workspace_embedding_generation_refresh_ready
    ON workspace_embedding_generations(embedding_profile, updated_at, id)
    WHERE status = 'ready' AND creation_reason = 'corpus_refresh';

CREATE INDEX idx_document_versions_refresh_blockers
    ON document_versions(tenant_id, user_id)
    WHERE status = 'processing';

CREATE INDEX idx_document_embedding_jobs_refresh_blockers
    ON document_embedding_jobs(tenant_id, user_id)
    WHERE status IN ('pending', 'publishing', 'published', 'processing');

CREATE OR REPLACE FUNCTION protect_workspace_embedding_generation_identity()
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
       OR OLD.creation_reason IS DISTINCT FROM NEW.creation_reason
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'embedding generation identity and snapshot fields are immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP FUNCTION start_workspace_embedding_generation(TEXT, TEXT, VARCHAR);

CREATE FUNCTION start_workspace_embedding_generation(
    target_tenant_id TEXT,
    target_user_id TEXT,
    target_embedding_profile VARCHAR(255),
    target_creation_reason VARCHAR(30) DEFAULT 'operator'
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
    IF target_creation_reason NOT IN ('operator', 'corpus_refresh') THEN
        RAISE EXCEPTION 'unsupported embedding generation creation reason';
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
        expected_chunk_count, creation_reason
    ) VALUES (
        generation_id, target_tenant_id, target_user_id,
        target_embedding_profile, corpus_revision, expected_count,
        target_creation_reason
    );

    INSERT INTO chunk_embedding_vectors (
        generation_id, chunk_id, tenant_id, user_id, content_sha256,
        status, embedding, provider_metadata, embedded_at
    )
    SELECT
        generation_id,
        eligible.chunk_id,
        eligible.tenant_id,
        eligible.user_id,
        eligible.content_sha256,
        CASE WHEN reusable.embedding IS NULL THEN 'pending' ELSE 'embedded' END,
        reusable.embedding,
        CASE
            WHEN reusable.active_generation_id IS NOT NULL THEN
                jsonb_build_object(
                    'reuse_source', 'active_generation',
                    'source_generation_id', reusable.active_generation_id,
                    'embedding_profile', target_embedding_profile
                )
            WHEN reusable.embedding IS NOT NULL THEN
                jsonb_build_object(
                    'reuse_source', 'canonical_chunk',
                    'embedding_profile', target_embedding_profile
                )
            ELSE '{}'::jsonb
        END,
        CASE WHEN reusable.embedding IS NULL THEN NULL ELSE NOW() END
    FROM (
        SELECT chunk.id AS chunk_id, chunk.tenant_id, chunk.user_id,
               chunk.embedding AS canonical_embedding,
               chunk.embedding_profile AS canonical_profile,
               encode(
                   digest(convert_to(chunk.content, 'UTF8'), 'sha256'),
                   'hex'
               ) AS content_sha256
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
    ) AS eligible
    LEFT JOIN LATERAL (
        SELECT candidate.embedding, active.id AS active_generation_id
        FROM workspace_embedding_generations AS active
        JOIN chunk_embedding_vectors AS candidate
          ON candidate.generation_id = active.id
         AND candidate.chunk_id = eligible.chunk_id
         AND candidate.content_sha256 = eligible.content_sha256
         AND candidate.status = 'embedded'
        WHERE target_creation_reason = 'corpus_refresh'
          AND active.tenant_id = target_tenant_id
          AND active.user_id = target_user_id
          AND active.embedding_profile = target_embedding_profile
          AND active.status = 'active'
          AND active.evaluation_report->>'evaluation_profile'
                = 'embedding_generation_integrity_v1'
          AND active.evaluation_report->>'evaluation_source'
                = 'database_authoritative'
          AND active.evaluation_report->>'decision' = 'approved'
          AND COALESCE(
                (active.evaluation_report->>'gates_passed')::BOOLEAN,
                FALSE
              ) IS TRUE
        LIMIT 1
    ) AS approved_active ON TRUE
    CROSS JOIN LATERAL (
        SELECT
            COALESCE(
                approved_active.embedding,
                CASE
                    WHEN target_creation_reason = 'corpus_refresh'
                     AND eligible.canonical_profile = target_embedding_profile
                    THEN eligible.canonical_embedding
                    ELSE NULL
                END
            ) AS embedding,
            approved_active.active_generation_id
    ) AS reusable
    ORDER BY eligible.chunk_id;

    GET DIAGNOSTICS inserted_count = ROW_COUNT;
    IF inserted_count <> expected_count THEN
        RAISE EXCEPTION 'embedding snapshot changed while it was being captured';
    END IF;

    PERFORM refresh_workspace_embedding_generation_counts(generation_id);
    RETURN generation_id;
END;
$$;

CREATE FUNCTION start_next_workspace_embedding_refresh(
    target_embedding_profile VARCHAR(255),
    quiet_period INTERVAL DEFAULT INTERVAL '30 seconds'
)
RETURNS UUID
LANGUAGE plpgsql
AS $$
DECLARE
    selected_generation RECORD;
    active_generation RECORD;
    refresh_generation_id UUID;
BEGIN
    IF quiet_period < INTERVAL '0 seconds'
       OR quiet_period > INTERVAL '1 hour' THEN
        RAISE EXCEPTION 'embedding refresh quiet period must be between 0 and 1 hour';
    END IF;

    SELECT active.id, active.tenant_id, active.user_id
    INTO selected_generation
    FROM workspace_embedding_generations AS active
    JOIN workspace_embedding_corpus_revisions AS corpus
      ON corpus.tenant_id = active.tenant_id
     AND corpus.user_id = active.user_id
    WHERE active.status = 'active'
      AND active.embedding_profile = target_embedding_profile
      AND active.source_corpus_revision < corpus.revision
      AND corpus.updated_at <= NOW() - quiet_period
      AND NOT EXISTS (
          SELECT 1
          FROM workspace_embedding_generations AS open_generation
          WHERE open_generation.tenant_id = active.tenant_id
            AND open_generation.user_id = active.user_id
            AND open_generation.status IN ('building', 'ready')
      )
      AND NOT EXISTS (
          SELECT 1
          FROM document_versions AS version
          WHERE version.tenant_id = active.tenant_id
            AND version.user_id = active.user_id
            AND version.status = 'processing'
      )
      AND NOT EXISTS (
          SELECT 1
          FROM document_versions AS version
          JOIN document_derivations AS derivation
            ON derivation.document_version_id = version.id
           AND derivation.id IN (
                version.current_derivation_id,
                version.pending_derivation_id
           )
          WHERE version.tenant_id = active.tenant_id
            AND version.user_id = active.user_id
            AND derivation.status = 'processing'
      )
      AND NOT EXISTS (
          SELECT 1
          FROM document_embedding_jobs AS job
          JOIN document_versions AS version
            ON version.id = job.document_version_id
           AND job.derivation_id IN (
                version.current_derivation_id,
                version.pending_derivation_id
           )
          WHERE job.tenant_id = active.tenant_id
            AND job.user_id = active.user_id
            AND job.status IN ('pending', 'publishing', 'published', 'processing')
      )
    ORDER BY corpus.updated_at, active.updated_at, active.id
    LIMIT 1;

    IF selected_generation.id IS NULL THEN
        RETURN NULL;
    END IF;

    -- Every corpus lifecycle path takes this key before mutable rows. Select a
    -- candidate optimistically, acquire the scope key, then revalidate under a
    -- row lock to avoid lock-order inversions with ingestion or activation.
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            selected_generation.tenant_id || chr(31)
            || selected_generation.user_id,
            0
        )
    );

    SELECT active.id, active.tenant_id, active.user_id
    INTO active_generation
    FROM workspace_embedding_generations AS active
    JOIN workspace_embedding_corpus_revisions AS corpus
      ON corpus.tenant_id = active.tenant_id
     AND corpus.user_id = active.user_id
    WHERE active.id = selected_generation.id
      AND active.status = 'active'
      AND active.embedding_profile = target_embedding_profile
      AND active.source_corpus_revision < corpus.revision
      AND corpus.updated_at <= NOW() - quiet_period
      AND NOT EXISTS (
          SELECT 1
          FROM workspace_embedding_generations AS open_generation
          WHERE open_generation.tenant_id = active.tenant_id
            AND open_generation.user_id = active.user_id
            AND open_generation.status IN ('building', 'ready')
      )
      AND NOT EXISTS (
          SELECT 1 FROM document_versions AS version
          WHERE version.tenant_id = active.tenant_id
            AND version.user_id = active.user_id
            AND version.status = 'processing'
      )
      AND NOT EXISTS (
          SELECT 1
          FROM document_versions AS version
          JOIN document_derivations AS derivation
            ON derivation.document_version_id = version.id
           AND derivation.id IN (
                version.current_derivation_id,
                version.pending_derivation_id
           )
          WHERE version.tenant_id = active.tenant_id
            AND version.user_id = active.user_id
            AND derivation.status = 'processing'
      )
      AND NOT EXISTS (
          SELECT 1
          FROM document_embedding_jobs AS job
          JOIN document_versions AS version
            ON version.id = job.document_version_id
           AND job.derivation_id IN (
                version.current_derivation_id,
                version.pending_derivation_id
           )
          WHERE job.tenant_id = active.tenant_id
            AND job.user_id = active.user_id
            AND job.status IN ('pending', 'publishing', 'published', 'processing')
      )
    FOR UPDATE OF active;

    IF active_generation.id IS NULL THEN
        RETURN NULL;
    END IF;

    refresh_generation_id := start_workspace_embedding_generation(
        active_generation.tenant_id,
        active_generation.user_id,
        target_embedding_profile,
        'corpus_refresh'
    );
    RETURN refresh_generation_id;
END;
$$;

CREATE FUNCTION activate_next_workspace_embedding_refresh(
    target_embedding_profile VARCHAR(255),
    rollback_window INTERVAL DEFAULT INTERVAL '7 days'
)
RETURNS UUID
LANGUAGE plpgsql
AS $$
DECLARE
    selected_generation RECORD;
    refresh_generation RECORD;
BEGIN
    SELECT candidate.id, candidate.tenant_id, candidate.user_id
    INTO selected_generation
    FROM workspace_embedding_generations AS candidate
    JOIN workspace_embedding_generations AS active
      ON active.tenant_id = candidate.tenant_id
     AND active.user_id = candidate.user_id
     AND active.status = 'active'
     AND active.embedding_profile = candidate.embedding_profile
    WHERE candidate.status = 'ready'
      AND candidate.creation_reason = 'corpus_refresh'
      AND candidate.embedding_profile = target_embedding_profile
      AND candidate.evaluation_report->>'baseline_generation_id' = active.id::TEXT
    ORDER BY candidate.updated_at, candidate.id
    LIMIT 1;

    IF selected_generation.id IS NULL THEN
        RETURN NULL;
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            selected_generation.tenant_id || chr(31)
            || selected_generation.user_id,
            0
        )
    );

    SELECT candidate.id
    INTO refresh_generation
    FROM workspace_embedding_generations AS candidate
    JOIN workspace_embedding_generations AS active
      ON active.tenant_id = candidate.tenant_id
     AND active.user_id = candidate.user_id
     AND active.status = 'active'
     AND active.embedding_profile = candidate.embedding_profile
    WHERE candidate.id = selected_generation.id
      AND candidate.status = 'ready'
      AND candidate.creation_reason = 'corpus_refresh'
      AND candidate.embedding_profile = target_embedding_profile
      AND candidate.evaluation_report->>'baseline_generation_id' = active.id::TEXT
    FOR UPDATE OF candidate;

    IF refresh_generation.id IS NULL THEN
        RETURN NULL;
    END IF;
    IF activate_workspace_embedding_generation(
        refresh_generation.id,
        rollback_window
    ) THEN
        RETURN refresh_generation.id;
    END IF;
    RETURN NULL;
END;
$$;

COMMENT ON COLUMN workspace_embedding_generations.creation_reason IS
    'Immutable provenance separating operator builds from automatic same-profile corpus refreshes.';
COMMENT ON FUNCTION start_workspace_embedding_generation(TEXT, TEXT, VARCHAR, VARCHAR) IS
    'Captures an exact workspace snapshot; system corpus refreshes reuse only immutable, profile-compatible vectors.';
COMMENT ON FUNCTION start_next_workspace_embedding_refresh(VARCHAR, INTERVAL) IS
    'Fairly starts one quiet, fully processed, revision-lagged same-profile corpus refresh.';
COMMENT ON FUNCTION activate_next_workspace_embedding_refresh(VARCHAR, INTERVAL) IS
    'Automatically activates one database-qualified system refresh whose approved baseline is still active.';
