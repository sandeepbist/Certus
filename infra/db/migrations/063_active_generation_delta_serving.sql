-- Keep an approved active generation serving while the live corpus advances.
-- Retrieval merges that immutable baseline with compatible chunk-table deltas;
-- open replacement snapshots still become stale and must be rebuilt.

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
    SET status = 'stale', stale_at = COALESCE(stale_at, NOW()),
        rollback_until = NULL, updated_at = NOW(),
        last_error = COALESCE(
            last_error,
            'The searchable workspace corpus changed while this replacement snapshot was open.'
        )
    WHERE tenant_id = target_tenant_id
      AND user_id = target_user_id
      AND status IN ('building', 'ready');
END;
$$;

COMMENT ON FUNCTION invalidate_workspace_embedding_scope(TEXT, TEXT) IS
    'Advances one serialized tenant/user corpus revision, stales open replacements, and preserves the active immutable baseline for generation-plus-delta retrieval.';
