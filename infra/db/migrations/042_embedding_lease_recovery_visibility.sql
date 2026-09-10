-- PostgreSQL data-modifying CTE siblings share one command snapshot, so the
-- candidate selector in migration 041 could not see a just-recovered lease
-- until the next poll. Run recovery and claiming as sequential PL/pgSQL
-- statements so an expired candidate is claimable immediately.

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
    WHERE id = target_generation_id
    FOR SHARE;
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

    PERFORM refresh_workspace_embedding_generation_counts(target_generation_id);
END;
$$;
