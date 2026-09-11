-- Replace caller-authored embedding-generation approvals with a database-owned
-- integrity qualification. This gate proves snapshot/vector correctness; it
-- deliberately does not claim semantic retrieval quality without gold labels.

DROP FUNCTION seal_workspace_embedding_generation(UUID, JSONB);

CREATE FUNCTION qualify_workspace_embedding_generation(
    target_generation_id UUID
)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    generation workspace_embedding_generations%ROWTYPE;
    profile embedding_profiles%ROWTYPE;
    current_revision BIGINT;
    active_generation_id UUID;
    row_record RECORD;
    membership_count INT := 0;
    invalid_state_count INT := 0;
    invalid_hash_count INT := 0;
    invalid_dimension_count INT := 0;
    zero_norm_count INT := 0;
    normalization_violation_count INT := 0;
    candidate_chain BYTEA := decode(repeat('00', 32), 'hex');
    baseline_chain BYTEA := decode(repeat('00', 32), 'hex');
    candidate_fingerprint TEXT;
    baseline_fingerprint TEXT;
    report JSONB;
    gates_passed BOOLEAN;
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
    WHERE tenant_id = generation.tenant_id
      AND user_id = generation.user_id
    FOR UPDATE;

    IF current_revision IS DISTINCT FROM generation.source_corpus_revision THEN
        RETURN FALSE;
    END IF;

    PERFORM refresh_workspace_embedding_generation_counts(target_generation_id);
    SELECT * INTO generation
    FROM workspace_embedding_generations
    WHERE id = target_generation_id;

    -- An unfinished generation is not a failed evaluation. The durable worker
    -- may safely retry qualification after the remaining candidates complete.
    IF generation.embedded_chunk_count <> generation.expected_chunk_count
       OR generation.failed_chunk_count <> 0 THEN
        RETURN FALSE;
    END IF;

    SELECT * INTO profile
    FROM embedding_profiles
    WHERE identifier = generation.embedding_profile;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'embedding generation profile is not registered';
    END IF;

    FOR row_record IN
        SELECT candidate.chunk_id,
               candidate.content_sha256,
               candidate.status,
               candidate.embedding,
               candidate.embedding_profile,
               source_chunk.content,
               source_chunk.embedding AS baseline_embedding,
               source_chunk.embedding_profile AS baseline_profile
        FROM chunk_embedding_vectors AS candidate
        JOIN chunks AS source_chunk
          ON source_chunk.id = candidate.chunk_id
         AND source_chunk.tenant_id = candidate.tenant_id
         AND source_chunk.user_id = candidate.user_id
        WHERE candidate.generation_id = target_generation_id
        ORDER BY candidate.chunk_id
    LOOP
        membership_count := membership_count + 1;
        IF row_record.status <> 'embedded'
           OR row_record.embedding IS NULL
           OR row_record.embedding_profile IS DISTINCT FROM generation.embedding_profile THEN
            invalid_state_count := invalid_state_count + 1;
            CONTINUE;
        END IF;
        IF row_record.content_sha256 IS DISTINCT FROM encode(
            digest(convert_to(row_record.content, 'UTF8'), 'sha256'), 'hex'
        ) THEN
            invalid_hash_count := invalid_hash_count + 1;
        END IF;
        IF vector_dims(row_record.embedding) <> profile.dimensions THEN
            invalid_dimension_count := invalid_dimension_count + 1;
        END IF;
        IF vector_norm(row_record.embedding) <= 0.000000000001 THEN
            zero_norm_count := zero_norm_count + 1;
        END IF;
        IF profile.normalization_profile = 'l2_normalized:v1'
           AND abs(vector_norm(row_record.embedding) - 1.0) > 0.001 THEN
            normalization_violation_count := normalization_violation_count + 1;
        END IF;

        candidate_chain := digest(
            candidate_chain || digest(
                convert_to(
                    row_record.chunk_id::TEXT || chr(31)
                    || row_record.content_sha256 || chr(31)
                    || row_record.embedding::TEXT,
                    'UTF8'
                ),
                'sha256'
            ),
            'sha256'
        );
        IF row_record.baseline_embedding IS NOT NULL THEN
            baseline_chain := digest(
                baseline_chain || digest(
                    convert_to(
                        row_record.chunk_id::TEXT || chr(31)
                        || row_record.baseline_profile || chr(31)
                        || row_record.baseline_embedding::TEXT,
                        'UTF8'
                    ),
                    'sha256'
                ),
                'sha256'
            );
        END IF;
    END LOOP;

    candidate_fingerprint := encode(candidate_chain, 'hex');
    SELECT id INTO active_generation_id
    FROM workspace_embedding_generations
    WHERE tenant_id = generation.tenant_id
      AND user_id = generation.user_id
      AND status = 'active'
    FOR SHARE;

    IF active_generation_id IS NULL THEN
        baseline_fingerprint := encode(baseline_chain, 'hex');
    ELSE
        SELECT evaluation_report->>'candidate_fingerprint'
        INTO baseline_fingerprint
        FROM workspace_embedding_generations
        WHERE id = active_generation_id;
        IF COALESCE(baseline_fingerprint, '') !~ '^[0-9a-f]{64}$' THEN
            -- Legacy active generations may predate this evaluator. Hash their
            -- immutable vector rows without materializing a workspace-sized
            -- string or JSON value in memory.
            baseline_chain := decode(repeat('00', 32), 'hex');
            FOR row_record IN
                SELECT chunk_id, content_sha256, embedding
                FROM chunk_embedding_vectors
                WHERE generation_id = active_generation_id
                  AND status = 'embedded'
                ORDER BY chunk_id
            LOOP
                baseline_chain := digest(
                    baseline_chain || digest(
                        convert_to(
                            row_record.chunk_id::TEXT || chr(31)
                            || row_record.content_sha256 || chr(31)
                            || row_record.embedding::TEXT,
                            'UTF8'
                        ),
                        'sha256'
                    ),
                    'sha256'
                );
            END LOOP;
            baseline_fingerprint := encode(baseline_chain, 'hex');
        END IF;
    END IF;

    gates_passed := membership_count = generation.expected_chunk_count
        AND invalid_state_count = 0
        AND invalid_hash_count = 0
        AND invalid_dimension_count = 0
        AND zero_norm_count = 0
        AND normalization_violation_count = 0;

    report := jsonb_build_object(
        'schema_version', 2,
        'evaluation_profile', 'embedding_generation_integrity_v1',
        'evaluation_source', 'database_authoritative',
        'decision', CASE WHEN gates_passed THEN 'approved' ELSE 'rejected' END,
        'gates_passed', gates_passed,
        'quality_claim', 'not_evaluated',
        'evaluated_at', to_jsonb(NOW()),
        'source_corpus_revision', generation.source_corpus_revision,
        'embedding_profile', generation.embedding_profile,
        'baseline_generation_id', active_generation_id,
        'baseline_fingerprint', baseline_fingerprint,
        'candidate_fingerprint', candidate_fingerprint,
        'metrics', jsonb_build_object(
            'expected_chunk_count', generation.expected_chunk_count,
            'observed_chunk_count', membership_count,
            'invalid_state_count', invalid_state_count,
            'invalid_content_hash_count', invalid_hash_count,
            'invalid_dimension_count', invalid_dimension_count,
            'zero_norm_count', zero_norm_count,
            'normalization_violation_count', normalization_violation_count
        )
    );

    IF NOT gates_passed THEN
        UPDATE workspace_embedding_generations
        SET status = 'failed', evaluation_report = report,
            last_error = 'Embedding generation failed integrity qualification.',
            updated_at = NOW(), rollback_until = NULL
        WHERE id = target_generation_id AND status = 'building';
        RETURN FALSE;
    END IF;

    UPDATE workspace_embedding_generations
    SET status = 'ready', evaluation_report = report,
        sealed_at = NOW(), updated_at = NOW(), last_error = NULL
    WHERE id = target_generation_id AND status = 'building';
    RETURN FOUND;
END;
$$;

CREATE OR REPLACE FUNCTION activate_workspace_embedding_generation(
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
    IF generation.evaluation_report->>'evaluation_profile'
            IS DISTINCT FROM 'embedding_generation_integrity_v1'
       OR generation.evaluation_report->>'evaluation_source'
            IS DISTINCT FROM 'database_authoritative'
       OR generation.evaluation_report->>'decision' IS DISTINCT FROM 'approved'
       OR COALESCE(
            (generation.evaluation_report->>'gates_passed')::BOOLEAN,
            FALSE
       ) IS NOT TRUE
       OR COALESCE(generation.evaluation_report->>'candidate_fingerprint', '')
            !~ '^[0-9a-f]{64}$' THEN
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

COMMENT ON FUNCTION qualify_workspace_embedding_generation(UUID) IS
    'Database-authored integrity qualification for a complete immutable embedding snapshot; semantic retrieval quality remains explicitly not evaluated.';
COMMENT ON FUNCTION activate_workspace_embedding_generation(UUID, INTERVAL) IS
    'Atomically activates only a database-qualified embedding generation and retains its predecessor for a bounded rollback window.';
