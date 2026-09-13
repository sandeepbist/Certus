-- Bound replacement-index storage and tenant-wide provider concurrency at the
-- database authority. Defaults are deliberately generous enough for the
-- local MVP while still preventing an accidental unbounded reindex.

ALTER TABLE tenant_config
    ADD COLUMN max_embedding_generation_chunks INT DEFAULT 100000,
    ADD COLUMN max_embedding_generation_inflight_chunks INT DEFAULT 100,
    ADD CONSTRAINT tenant_config_embedding_generation_chunks_check CHECK (
        max_embedding_generation_chunks IS NULL
        OR max_embedding_generation_chunks BETWEEN 0 AND 10000000
    ),
    ADD CONSTRAINT tenant_config_embedding_generation_inflight_check CHECK (
        max_embedding_generation_inflight_chunks IS NULL
        OR max_embedding_generation_inflight_chunks BETWEEN 1 AND 10000
    );

CREATE FUNCTION inspect_workspace_embedding_generation_capacity(
    target_tenant_id TEXT,
    target_user_id TEXT
)
RETURNS TABLE (
    eligible_chunk_count BIGINT,
    chunk_limit INT,
    accepted BOOLEAN
)
LANGUAGE sql
STABLE
AS $$
    WITH configured AS (
        SELECT
            CASE
                WHEN config.organization_id IS NULL THEN 100000
                ELSE config.max_embedding_generation_chunks
            END AS chunk_limit
        FROM (VALUES (1)) AS singleton(value)
        LEFT JOIN tenant_config AS config
          ON config.organization_id = target_tenant_id
    ), eligible AS (
        SELECT COUNT(*) AS chunk_count
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
    )
    SELECT eligible.chunk_count,
           configured.chunk_limit,
           configured.chunk_limit IS NULL
             OR eligible.chunk_count <= configured.chunk_limit
    FROM eligible CROSS JOIN configured;
$$;

CREATE FUNCTION enforce_workspace_embedding_generation_capacity()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    configured_limit INT;
BEGIN
    SELECT CASE
        WHEN config.organization_id IS NULL THEN 100000
        ELSE config.max_embedding_generation_chunks
    END
    INTO configured_limit
    FROM (VALUES (1)) AS singleton(value)
    LEFT JOIN tenant_config AS config
      ON config.organization_id = NEW.tenant_id;

    IF configured_limit IS NOT NULL
       AND NEW.expected_chunk_count > configured_limit THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P2001',
            MESSAGE = 'embedding generation chunk capacity exceeded';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_enforce_workspace_embedding_generation_capacity
    BEFORE INSERT ON workspace_embedding_generations
    FOR EACH ROW EXECUTE FUNCTION enforce_workspace_embedding_generation_capacity();

ALTER FUNCTION start_workspace_embedding_generation(TEXT, TEXT, VARCHAR, VARCHAR)
    RENAME TO start_workspace_embedding_generation_snapshot;

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
    capacity RECORD;
BEGIN
    SELECT * INTO capacity
    FROM inspect_workspace_embedding_generation_capacity(
        target_tenant_id, target_user_id
    );
    IF capacity.accepted IS DISTINCT FROM true THEN
        RETURN NULL;
    END IF;

    BEGIN
        RETURN start_workspace_embedding_generation_snapshot(
            target_tenant_id,
            target_user_id,
            target_embedding_profile,
            target_creation_reason
        );
    EXCEPTION WHEN SQLSTATE 'P2001' THEN
        -- Corpus membership can grow after the optimistic inspection but
        -- before the existing scope lock. The insertion trigger is the final
        -- authority and converts that race into an honest admission refusal.
        RETURN NULL;
    END;
END;
$$;

CREATE OR REPLACE FUNCTION start_next_workspace_embedding_refresh(
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
      AND (
          SELECT capacity.accepted
          FROM inspect_workspace_embedding_generation_capacity(
              active.tenant_id, active.user_id
          ) AS capacity
      )
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
      AND (
          SELECT capacity.accepted
          FROM inspect_workspace_embedding_generation_capacity(
              active.tenant_id, active.user_id
          ) AS capacity
      )
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

CREATE OR REPLACE FUNCTION claim_chunk_embedding_vectors(
    target_generation_id UUID,
    target_lease_owner UUID,
    batch_size INT DEFAULT 20,
    lease_seconds INT DEFAULT 300
)
RETURNS TABLE (
    chunk_id UUID,
    content TEXT,
    contextualized_content TEXT,
    content_sha256 VARCHAR(64),
    attempt_count INT
)
LANGUAGE plpgsql
AS $$
DECLARE
    generation_status VARCHAR(20);
    generation_paused BOOLEAN;
    generation_tenant_id TEXT;
    configured_inflight_limit INT;
    current_inflight_count BIGINT;
    effective_batch_size INT;
BEGIN
    IF target_lease_owner IS NULL THEN
        RAISE EXCEPTION 'embedding candidate claim requires a lease owner';
    END IF;
    IF batch_size < 1 OR batch_size > 100 THEN
        RAISE EXCEPTION 'embedding candidate batch size must be between 1 and 100';
    END IF;
    IF lease_seconds < 60 OR lease_seconds > 3600 THEN
        RAISE EXCEPTION 'embedding candidate lease must be between 60 and 3600 seconds';
    END IF;

    SELECT status, is_paused, tenant_id
    INTO generation_status, generation_paused, generation_tenant_id
    FROM workspace_embedding_generations
    WHERE id = target_generation_id
    FOR SHARE;
    IF generation_status IS DISTINCT FROM 'building'
       OR generation_paused IS DISTINCT FROM false THEN
        RETURN;
    END IF;

    -- This quota lock is deliberately separate from the workspace lifecycle
    -- key. Claims for different profiles in one tenant serialize only their
    -- short admission transaction and cannot deadlock pause/invalidation.
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'certus:embedding-generation-capacity:' || generation_tenant_id,
        0
    ));

    UPDATE chunk_embedding_vectors AS expired
    SET status = 'pending', lease_owner = NULL, leased_at = NULL,
        available_at = NOW(), updated_at = NOW()
    WHERE expired.generation_id = target_generation_id
      AND expired.status = 'processing'
      AND expired.leased_at < NOW() - (lease_seconds * INTERVAL '1 second');

    SELECT max_embedding_generation_inflight_chunks
    INTO configured_inflight_limit
    FROM tenant_config
    WHERE organization_id = generation_tenant_id;
    IF NOT FOUND THEN
        configured_inflight_limit := 100;
    END IF;

    IF configured_inflight_limit IS NULL THEN
        effective_batch_size := batch_size;
    ELSE
        SELECT COUNT(*) INTO current_inflight_count
        FROM chunk_embedding_vectors AS inflight
        WHERE inflight.tenant_id = generation_tenant_id
          AND inflight.status = 'processing'
          AND inflight.leased_at >= NOW()
                - (lease_seconds * INTERVAL '1 second');
        effective_batch_size := LEAST(
            batch_size,
            GREATEST(configured_inflight_limit - current_inflight_count, 0)
        )::INT;
    END IF;
    IF effective_batch_size < 1 THEN
        RETURN;
    END IF;

    RETURN QUERY
    WITH candidates AS (
        SELECT candidate.generation_id, candidate.chunk_id
        FROM chunk_embedding_vectors AS candidate
        WHERE candidate.generation_id = target_generation_id
          AND candidate.status IN ('pending', 'failed')
          AND candidate.available_at <= NOW()
        ORDER BY candidate.chunk_id
        FOR UPDATE SKIP LOCKED
        LIMIT effective_batch_size
    ), claimed AS (
        UPDATE chunk_embedding_vectors AS candidate
        SET status = 'processing', lease_owner = target_lease_owner,
            leased_at = NOW(), last_attempt_at = NOW(),
            attempt_count = candidate.attempt_count + 1,
            last_error_code = NULL, last_error = NULL, updated_at = NOW()
        FROM candidates
        WHERE candidate.generation_id = candidates.generation_id
          AND candidate.chunk_id = candidates.chunk_id
        RETURNING candidate.chunk_id, candidate.content_sha256,
                  candidate.attempt_count
    )
    SELECT claimed.chunk_id, source_chunk.content,
           source_chunk.contextualized_content, claimed.content_sha256,
           claimed.attempt_count
    FROM claimed
    JOIN chunks AS source_chunk ON source_chunk.id = claimed.chunk_id
    ORDER BY claimed.chunk_id;
END;
$$;

COMMENT ON COLUMN tenant_config.max_embedding_generation_chunks IS
    'Maximum eligible chunks in one user-scoped embedding generation; NULL explicitly disables this ceiling.';
COMMENT ON COLUMN tenant_config.max_embedding_generation_inflight_chunks IS
    'Tenant-wide maximum concurrently leased embedding-generation candidates; NULL explicitly disables this ceiling.';
COMMENT ON FUNCTION inspect_workspace_embedding_generation_capacity(TEXT, TEXT) IS
    'Reports exact current snapshot size and the configured admission decision.';
COMMENT ON FUNCTION start_workspace_embedding_generation(TEXT, TEXT, VARCHAR, VARCHAR) IS
    'Capacity-gated entry point for operator and automatic workspace embedding generations.';
COMMENT ON FUNCTION start_workspace_embedding_generation_snapshot(TEXT, TEXT, VARCHAR, VARCHAR) IS
    'Snapshot implementation behind the capacity-gated entry point; the insertion trigger remains the final authority.';
