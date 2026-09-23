-- Fail without changing legacy rows that reference another tenant or user.
DO $$
DECLARE
    invalid_source_run_references BIGINT;
    invalid_superseded_references BIGINT;
BEGIN
    SELECT COUNT(*)
    INTO invalid_source_run_references
    FROM memories AS memory
    LEFT JOIN agent_runs AS source_run ON source_run.id = memory.source_run_id
    WHERE memory.source_run_id IS NOT NULL
      AND (
          source_run.id IS NULL
          OR memory.tenant_id IS DISTINCT FROM source_run.tenant_id
          OR memory.user_id IS DISTINCT FROM source_run.user_id
      );

    SELECT COUNT(*)
    INTO invalid_superseded_references
    FROM memories AS memory
    LEFT JOIN memories AS superseding_memory ON superseding_memory.id = memory.superseded_by
    WHERE memory.superseded_by IS NOT NULL
      AND (
          superseding_memory.id IS NULL
          OR memory.tenant_id IS DISTINCT FROM superseding_memory.tenant_id
          OR memory.user_id IS DISTINCT FROM superseding_memory.user_id
      );

    IF invalid_source_run_references > 0 OR invalid_superseded_references > 0 THEN
        RAISE EXCEPTION
            'Cannot install memory scope constraints: % cross-scope source_run_id reference(s), % cross-scope superseded_by reference(s)',
            invalid_source_run_references,
            invalid_superseded_references;
    END IF;
END;
$$;

ALTER TABLE agent_runs
    ADD CONSTRAINT agent_runs_id_tenant_user_key UNIQUE (id, tenant_id, user_id);

ALTER TABLE memories
    ADD CONSTRAINT memories_id_tenant_user_key UNIQUE (id, tenant_id, user_id);

ALTER TABLE memories
    DROP CONSTRAINT IF EXISTS memories_source_run_id_fkey,
    DROP CONSTRAINT IF EXISTS memories_superseded_by_fkey,
    ADD CONSTRAINT memories_source_run_scope_fkey
        FOREIGN KEY (source_run_id, tenant_id, user_id)
        REFERENCES agent_runs (id, tenant_id, user_id)
        ON DELETE SET NULL (source_run_id),
    ADD CONSTRAINT memories_superseded_by_scope_fkey
        FOREIGN KEY (superseded_by, tenant_id, user_id)
        REFERENCES memories (id, tenant_id, user_id);

ALTER TABLE memories ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS memories_scope_isolation ON memories;
CREATE POLICY memories_scope_isolation ON memories
    FOR ALL
    USING (
        tenant_id = NULLIF(current_setting('app.tenant_id', true), '')
        AND user_id = NULLIF(current_setting('app.user_id', true), '')
    )
    WITH CHECK (
        tenant_id = NULLIF(current_setting('app.tenant_id', true), '')
        AND user_id = NULLIF(current_setting('app.user_id', true), '')
    );
