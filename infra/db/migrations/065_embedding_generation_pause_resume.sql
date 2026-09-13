-- Give operators a durable pause boundary without adding another generation
-- lifecycle state. A paused build remains an open snapshot (so uniqueness and
-- corpus invalidation still apply), while workers and claims exclude it.

ALTER TABLE workspace_embedding_generations
    ADD COLUMN is_paused BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN last_paused_at TIMESTAMPTZ,
    ADD COLUMN last_resumed_at TIMESTAMPTZ,
    ADD COLUMN last_pause_reason TEXT,
    ADD CONSTRAINT workspace_embedding_generations_pause_state_check CHECK (
        (NOT is_paused OR (status = 'building' AND last_paused_at IS NOT NULL))
        AND (last_pause_reason IS NULL OR octet_length(last_pause_reason) <= 2000)
        AND (last_resumed_at IS NULL OR last_paused_at IS NOT NULL)
    );

DROP INDEX idx_workspace_embedding_generation_worker_dispatch;
CREATE INDEX idx_workspace_embedding_generation_worker_dispatch
    ON workspace_embedding_generations(embedding_profile, updated_at, id)
    WHERE status = 'building' AND is_paused = false;

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

    SELECT status, is_paused INTO generation_status, generation_paused
    FROM workspace_embedding_generations
    WHERE id = target_generation_id
    FOR SHARE;
    IF generation_status IS DISTINCT FROM 'building'
       OR generation_paused IS DISTINCT FROM false THEN
        RETURN;
    END IF;

    UPDATE chunk_embedding_vectors AS expired
    SET status = 'pending', lease_owner = NULL, leased_at = NULL,
        available_at = NOW(), updated_at = NOW()
    WHERE expired.generation_id = target_generation_id
      AND expired.status = 'processing'
      AND expired.leased_at < NOW() - (lease_seconds * INTERVAL '1 second');

    RETURN QUERY
    WITH candidates AS (
        SELECT candidate.generation_id, candidate.chunk_id
        FROM chunk_embedding_vectors AS candidate
        WHERE candidate.generation_id = target_generation_id
          AND candidate.status IN ('pending', 'failed')
          AND candidate.available_at <= NOW()
        ORDER BY candidate.chunk_id
        FOR UPDATE SKIP LOCKED
        LIMIT batch_size
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

CREATE FUNCTION pause_workspace_embedding_generation(
    target_generation_id UUID,
    target_tenant_id TEXT,
    target_user_id TEXT,
    pause_reason TEXT DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    generation_scope RECORD;
    normalized_reason TEXT := NULLIF(btrim(pause_reason), '');
BEGIN
    IF btrim(COALESCE(target_tenant_id, '')) = ''
       OR btrim(COALESCE(target_user_id, '')) = '' THEN
        RAISE EXCEPTION 'embedding generation pause requires tenant and user identity';
    END IF;
    IF normalized_reason IS NOT NULL
       AND octet_length(normalized_reason) > 2000 THEN
        RAISE EXCEPTION 'embedding generation pause reason is too large';
    END IF;

    SELECT tenant_id, user_id INTO generation_scope
    FROM workspace_embedding_generations
    WHERE id = target_generation_id
      AND tenant_id = target_tenant_id
      AND user_id = target_user_id;
    IF generation_scope.tenant_id IS NULL THEN
        RETURN FALSE;
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended(target_tenant_id || chr(31) || target_user_id, 0)
    );

    PERFORM 1
    FROM workspace_embedding_generations
    WHERE id = target_generation_id
      AND tenant_id = target_tenant_id
      AND user_id = target_user_id
      AND status = 'building'
      AND is_paused = false
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    UPDATE workspace_embedding_generations
    SET is_paused = true,
        last_paused_at = NOW(),
        last_pause_reason = normalized_reason,
        updated_at = NOW()
    WHERE id = target_generation_id;

    -- In-flight provider responses lose ownership immediately. Resume can
    -- reclaim these rows without waiting for the old lease timeout.
    UPDATE chunk_embedding_vectors
    SET status = 'pending', lease_owner = NULL, leased_at = NULL,
        available_at = NOW(), updated_at = NOW()
    WHERE generation_id = target_generation_id
      AND status = 'processing';
    RETURN TRUE;
END;
$$;

CREATE FUNCTION resume_workspace_embedding_generation(
    target_generation_id UUID,
    target_tenant_id TEXT,
    target_user_id TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    generation workspace_embedding_generations%ROWTYPE;
    current_revision BIGINT;
BEGIN
    IF btrim(COALESCE(target_tenant_id, '')) = ''
       OR btrim(COALESCE(target_user_id, '')) = '' THEN
        RAISE EXCEPTION 'embedding generation resume requires tenant and user identity';
    END IF;

    PERFORM 1
    FROM workspace_embedding_generations
    WHERE id = target_generation_id
      AND tenant_id = target_tenant_id
      AND user_id = target_user_id;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended(target_tenant_id || chr(31) || target_user_id, 0)
    );

    SELECT * INTO generation
    FROM workspace_embedding_generations
    WHERE id = target_generation_id
      AND tenant_id = target_tenant_id
      AND user_id = target_user_id
    FOR UPDATE;
    IF NOT FOUND OR generation.status <> 'building'
       OR generation.is_paused IS DISTINCT FROM true THEN
        RETURN FALSE;
    END IF;

    SELECT revision INTO current_revision
    FROM workspace_embedding_corpus_revisions
    WHERE tenant_id = target_tenant_id AND user_id = target_user_id
    FOR UPDATE;

    IF current_revision IS DISTINCT FROM generation.source_corpus_revision THEN
        UPDATE workspace_embedding_generations
        SET status = 'stale', is_paused = false,
            stale_at = COALESCE(stale_at, NOW()), rollback_until = NULL,
            last_error = COALESCE(
                last_error,
                'The searchable workspace corpus changed while this generation was paused.'
            ),
            updated_at = NOW()
        WHERE id = target_generation_id;
        RETURN FALSE;
    END IF;

    UPDATE workspace_embedding_generations
    SET is_paused = false, last_resumed_at = NOW(), updated_at = NOW()
    WHERE id = target_generation_id;
    RETURN TRUE;
END;
$$;

CREATE OR REPLACE FUNCTION invalidate_workspace_embedding_scope(
    target_tenant_id TEXT,
    target_user_id TEXT
)
RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
    IF btrim(COALESCE(target_tenant_id, '')) = ''
       OR btrim(COALESCE(target_user_id, '')) = '' THEN
        RAISE EXCEPTION 'embedding corpus invalidation requires tenant and user identity';
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended(target_tenant_id || chr(31) || target_user_id, 0)
    );

    INSERT INTO workspace_embedding_corpus_revisions (
        tenant_id, user_id, revision, updated_at
    ) VALUES (
        target_tenant_id, target_user_id, 1, NOW()
    )
    ON CONFLICT (tenant_id, user_id) DO UPDATE
    SET revision = workspace_embedding_corpus_revisions.revision + 1,
        updated_at = NOW();

    UPDATE workspace_embedding_generations
    SET status = 'stale', is_paused = false,
        stale_at = COALESCE(stale_at, NOW()), rollback_until = NULL,
        updated_at = NOW(),
        last_error = COALESCE(
            last_error,
            'The searchable workspace corpus changed while this replacement snapshot was open.'
        )
    WHERE tenant_id = target_tenant_id
      AND user_id = target_user_id
      AND status IN ('building', 'ready');
END;
$$;

COMMENT ON COLUMN workspace_embedding_generations.is_paused IS
    'Durable worker-admission substate; paused generations remain open and corpus-revision fenced.';
COMMENT ON FUNCTION pause_workspace_embedding_generation(UUID, TEXT, TEXT, TEXT) IS
    'Pauses one scoped building generation and immediately releases every in-flight candidate lease.';
COMMENT ON FUNCTION resume_workspace_embedding_generation(UUID, TEXT, TEXT) IS
    'Resumes one scoped generation only while its captured corpus revision remains current.';
