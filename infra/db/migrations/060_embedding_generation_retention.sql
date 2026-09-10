-- Reclaim reproducible terminal vector generations without weakening active
-- serving or a still-valid rollback window.

CREATE FUNCTION prune_embedding_generations(
    terminal_retention_days INT DEFAULT 30,
    batch_size INT DEFAULT 25
)
RETURNS TABLE(detached_predecessors INT, deleted_generations INT)
LANGUAGE plpgsql
AS $$
DECLARE
    detached_count INT := 0;
    deleted_count INT := 0;
BEGIN
    IF terminal_retention_days < 1 OR terminal_retention_days > 3650 THEN
        RAISE EXCEPTION 'embedding generation retention must be between 1 and 3650 days';
    END IF;
    IF batch_size < 1 OR batch_size > 1000 THEN
        RAISE EXCEPTION 'embedding generation prune batch must be between 1 and 1000';
    END IF;

    WITH expired_links AS (
        SELECT generation.id
        FROM workspace_embedding_generations AS generation
        WHERE generation.status = 'active'
          AND generation.previous_generation_id IS NOT NULL
          AND generation.rollback_until <= NOW()
        ORDER BY generation.rollback_until, generation.id
        FOR UPDATE SKIP LOCKED
        LIMIT batch_size
    )
    UPDATE workspace_embedding_generations AS generation
    SET previous_generation_id = NULL,
        rollback_until = NULL,
        updated_at = NOW()
    FROM expired_links
    WHERE generation.id = expired_links.id;
    GET DIAGNOSTICS detached_count = ROW_COUNT;

    WITH expired AS (
        SELECT generation.id
        FROM workspace_embedding_generations AS generation
        WHERE (
            generation.status IN ('failed', 'cancelled', 'stale', 'rolled_back')
            AND generation.updated_at
                < NOW() - (terminal_retention_days * INTERVAL '1 day')
        ) OR (
            generation.status = 'retired'
            AND generation.retain_until IS NOT NULL
            AND generation.retain_until <= NOW()
            AND NOT EXISTS (
                SELECT 1
                FROM workspace_embedding_generations AS successor
                WHERE successor.previous_generation_id = generation.id
            )
        )
        ORDER BY COALESCE(generation.retain_until, generation.updated_at),
                 generation.id
        FOR UPDATE SKIP LOCKED
        LIMIT batch_size
    )
    DELETE FROM workspace_embedding_generations AS generation
    USING expired
    WHERE generation.id = expired.id;
    GET DIAGNOSTICS deleted_count = ROW_COUNT;

    RETURN QUERY SELECT detached_count, deleted_count;
END;
$$;

CREATE INDEX idx_workspace_embedding_generation_terminal_retention
    ON workspace_embedding_generations(updated_at, id)
    WHERE status IN ('failed', 'cancelled', 'stale', 'rolled_back');

CREATE INDEX idx_workspace_embedding_generation_retired_retention
    ON workspace_embedding_generations(retain_until, id)
    WHERE status = 'retired' AND retain_until IS NOT NULL;

COMMENT ON FUNCTION prune_embedding_generations(INT, INT) IS
    'Detaches expired rollback predecessors and deletes only bounded batches of terminal or expired-retired generations; active, building, and ready generations are never deleted.';
