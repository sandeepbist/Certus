-- Serialize corpus invalidation with generation start/qualification/activation
-- on the same tenant/user advisory key. This prevents opposite row-lock order
-- between a concurrent upload/version change and a generation cutover.

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
            'The searchable workspace corpus changed after this snapshot was captured.'
        )
    WHERE tenant_id = target_tenant_id
      AND user_id = target_user_id
      AND status IN ('building', 'ready', 'active');
END;
$$;

COMMENT ON FUNCTION invalidate_workspace_embedding_scope(TEXT, TEXT) IS
    'Serializes a tenant/user corpus revision change with generation lifecycle operations, then stales every open or active snapshot.';
