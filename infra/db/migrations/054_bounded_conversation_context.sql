-- Preserve bounded dialogue context separately from typed citation evidence.
ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS conversation_context JSONB NOT NULL DEFAULT
        '{"canonical_sha256":"3c2b0debb20e651435956dc5eed0f1a4b2b2d1930b14cd59ae6e7d786cf28078","profile":"certus_conversation_context:v1","turn_count":0,"turns":[]}'::jsonb,
    ADD COLUMN IF NOT EXISTS conversation_context_db_sha256 TEXT;

ALTER TABLE agent_runs
    DROP CONSTRAINT IF EXISTS agent_runs_conversation_context_shape_check;
ALTER TABLE agent_runs
    ADD CONSTRAINT agent_runs_conversation_context_shape_check CHECK (
        jsonb_typeof(conversation_context) = 'object'
        AND pg_column_size(conversation_context) <= 32768
    );

CREATE OR REPLACE FUNCTION set_agent_run_conversation_context_fingerprint()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.conversation_context_db_sha256 := encode(
        digest(convert_to(NEW.conversation_context::text, 'UTF8'), 'sha256'),
        'hex'
    );
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_set_agent_run_conversation_context_fingerprint ON agent_runs;
CREATE TRIGGER trg_set_agent_run_conversation_context_fingerprint
    BEFORE INSERT OR UPDATE OF conversation_context
    ON agent_runs
    FOR EACH ROW EXECUTE FUNCTION set_agent_run_conversation_context_fingerprint();

UPDATE agent_runs SET conversation_context = conversation_context;

ALTER TABLE agent_runs
    ALTER COLUMN conversation_context_db_sha256 SET NOT NULL;

CREATE OR REPLACE FUNCTION protect_completed_agent_run_conversation_context()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status = 'completed'
       AND OLD.conversation_context IS DISTINCT FROM NEW.conversation_context THEN
        RAISE EXCEPTION 'completed agent-run conversation context is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_protect_completed_agent_run_conversation_context ON agent_runs;
CREATE TRIGGER trg_protect_completed_agent_run_conversation_context
    BEFORE UPDATE OF conversation_context
    ON agent_runs
    FOR EACH ROW EXECUTE FUNCTION protect_completed_agent_run_conversation_context();

-- Support bounded, tenant-scoped loading of completed original chat turns.
CREATE INDEX IF NOT EXISTS idx_agent_runs_session_context
    ON agent_runs (tenant_id, user_id, session_id, created_at DESC, id DESC)
    WHERE status = 'completed' AND replay_of_run_id IS NULL;
