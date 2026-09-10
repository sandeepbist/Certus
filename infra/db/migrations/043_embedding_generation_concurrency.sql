-- Keep incompatible coordinate spaces out of one ANN graph and make progress
-- accounting O(1) and concurrency-safe. A later serving migration must create
-- and prove a physical index scoped to one compatible profile/generation.

DROP INDEX idx_chunk_embedding_vectors_hnsw;

CREATE FUNCTION account_chunk_embedding_vector_transition()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    embedded_delta INT;
    failed_delta INT;
BEGIN
    embedded_delta := (NEW.status = 'embedded')::INT - (OLD.status = 'embedded')::INT;
    failed_delta := (NEW.status = 'failed')::INT - (OLD.status = 'failed')::INT;
    IF embedded_delta <> 0 OR failed_delta <> 0 THEN
        UPDATE workspace_embedding_generations
        SET embedded_chunk_count = embedded_chunk_count + embedded_delta,
            failed_chunk_count = failed_chunk_count + failed_delta,
            updated_at = NOW()
        WHERE id = NEW.generation_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_account_chunk_embedding_vector_transition
    AFTER UPDATE OF status ON chunk_embedding_vectors
    FOR EACH ROW
    WHEN (OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION account_chunk_embedding_vector_transition();

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

    SELECT status INTO generation_status
    FROM workspace_embedding_generations
    WHERE id = target_generation_id;
    IF generation_status IS DISTINCT FROM 'building' THEN
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

CREATE OR REPLACE FUNCTION record_chunk_embedding_vector(
    target_generation_id UUID,
    target_chunk_id UUID,
    target_lease_owner UUID,
    target_embedding vector(1536),
    target_provider_metadata JSONB DEFAULT '{}'
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    generation_status VARCHAR(20);
BEGIN
    SELECT status INTO generation_status
    FROM workspace_embedding_generations
    WHERE id = target_generation_id;

    IF generation_status IS DISTINCT FROM 'building' THEN
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
        lease_owner = NULL, leased_at = NULL,
        last_error_code = NULL, last_error = NULL,
        embedded_at = NOW(), updated_at = NOW()
    WHERE generation_id = target_generation_id
      AND chunk_id = target_chunk_id
      AND status = 'processing'
      AND lease_owner = target_lease_owner;
    RETURN FOUND;
END;
$$;

CREATE OR REPLACE FUNCTION record_chunk_embedding_failure(
    target_generation_id UUID,
    target_chunk_id UUID,
    target_lease_owner UUID,
    target_error_code VARCHAR(80),
    target_error TEXT,
    retry_delay INTERVAL DEFAULT INTERVAL '5 minutes'
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    generation_status VARCHAR(20);
BEGIN
    SELECT status INTO generation_status
    FROM workspace_embedding_generations
    WHERE id = target_generation_id;

    IF generation_status IS DISTINCT FROM 'building' THEN
        RETURN FALSE;
    END IF;
    IF btrim(COALESCE(target_error_code, '')) = ''
       OR btrim(COALESCE(target_error, '')) = '' THEN
        RAISE EXCEPTION 'embedding failure requires a code and message';
    END IF;
    IF retry_delay < INTERVAL '0 seconds'
       OR retry_delay > INTERVAL '24 hours' THEN
        RAISE EXCEPTION 'embedding retry delay must be between 0 and 24 hours';
    END IF;

    UPDATE chunk_embedding_vectors
    SET status = 'failed', embedding = NULL, provider_metadata = '{}',
        lease_owner = NULL, leased_at = NULL,
        last_error_code = left(target_error_code, 80),
        last_error = left(target_error, 2000),
        available_at = NOW() + retry_delay,
        embedded_at = NULL, updated_at = NOW()
    WHERE generation_id = target_generation_id
      AND chunk_id = target_chunk_id
      AND status = 'processing'
      AND lease_owner = target_lease_owner;
    RETURN FOUND;
END;
$$;

COMMENT ON TABLE chunk_embedding_vectors IS
    'Shadow vectors for one immutable generation snapshot. No cross-profile ANN index exists; serving must create and prove a compatible physical index before cutover.';
