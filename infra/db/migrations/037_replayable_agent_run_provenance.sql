-- Freeze the exact ordered evidence pack and grounded-generation request profile
-- for every new v1 completed answer. Document text remains authoritative in the
-- immutable chunk/parsed/source lineage; only non-document evidence without an
-- equivalent immutable authority is snapshotted in the bounded manifest.

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS evidence_manifest JSONB NOT NULL DEFAULT
        '{"profile":"legacy_unavailable:v0","source_count":0,"sources":[]}'::jsonb,
    ADD COLUMN IF NOT EXISTS generation_profile JSONB NOT NULL DEFAULT
        '{"profile":"legacy_unavailable:v0"}'::jsonb,
    ADD COLUMN IF NOT EXISTS replay_of_run_id UUID REFERENCES agent_runs(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS replay_mode TEXT NOT NULL DEFAULT 'original';

CREATE OR REPLACE FUNCTION protect_completed_agent_run_provenance()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status = 'completed'
       AND (
            OLD.evidence_manifest IS DISTINCT FROM NEW.evidence_manifest
            OR OLD.generation_profile IS DISTINCT FROM NEW.generation_profile
       ) THEN
        RAISE EXCEPTION 'completed agent-run provenance is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_protect_completed_agent_run_provenance ON agent_runs;
CREATE TRIGGER trg_protect_completed_agent_run_provenance
    BEFORE UPDATE OF evidence_manifest, generation_profile
    ON agent_runs
    FOR EACH ROW EXECUTE FUNCTION protect_completed_agent_run_provenance();

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS evidence_manifest_db_sha256 TEXT,
    ADD COLUMN IF NOT EXISTS generation_profile_db_sha256 TEXT;

CREATE OR REPLACE FUNCTION set_agent_run_provenance_fingerprints()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.evidence_manifest_db_sha256 := encode(
        digest(convert_to(NEW.evidence_manifest::text, 'UTF8'), 'sha256'),
        'hex'
    );
    NEW.generation_profile_db_sha256 := encode(
        digest(convert_to(NEW.generation_profile::text, 'UTF8'), 'sha256'),
        'hex'
    );
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_set_agent_run_provenance_fingerprints ON agent_runs;
CREATE TRIGGER trg_set_agent_run_provenance_fingerprints
    BEFORE INSERT OR UPDATE OF evidence_manifest, generation_profile
    ON agent_runs
    FOR EACH ROW EXECUTE FUNCTION set_agent_run_provenance_fingerprints();

UPDATE agent_runs
SET evidence_manifest = evidence_manifest,
    generation_profile = generation_profile;

ALTER TABLE agent_runs
    ALTER COLUMN evidence_manifest_db_sha256 SET NOT NULL,
    ALTER COLUMN generation_profile_db_sha256 SET NOT NULL;

ALTER TABLE agent_runs
    DROP CONSTRAINT IF EXISTS agent_runs_evidence_manifest_shape_check,
    DROP CONSTRAINT IF EXISTS agent_runs_generation_profile_shape_check,
    DROP CONSTRAINT IF EXISTS agent_runs_replay_mode_check,
    DROP CONSTRAINT IF EXISTS agent_runs_replay_parent_check;

ALTER TABLE agent_runs
    ADD CONSTRAINT agent_runs_evidence_manifest_shape_check CHECK (
        jsonb_typeof(evidence_manifest) = 'object'
        AND pg_column_size(evidence_manifest) <= 1048576
    ),
    ADD CONSTRAINT agent_runs_generation_profile_shape_check CHECK (
        jsonb_typeof(generation_profile) = 'object'
        AND pg_column_size(generation_profile) <= 65536
    ),
    ADD CONSTRAINT agent_runs_replay_mode_check CHECK (
        replay_mode IN ('original', 'frozen_evidence', 'fresh_retrieval')
    ),
    ADD CONSTRAINT agent_runs_replay_parent_check CHECK (
        (replay_mode = 'original' AND replay_of_run_id IS NULL)
        OR (replay_mode <> 'original' AND replay_of_run_id IS NOT NULL)
    );

CREATE INDEX IF NOT EXISTS idx_agent_runs_replay_parent
    ON agent_runs(replay_of_run_id, created_at DESC)
    WHERE replay_of_run_id IS NOT NULL;

CREATE OR REPLACE FUNCTION validate_replayable_agent_run_provenance()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    source JSONB;
    source_ref JSONB;
    claim JSONB;
    citation JSONB;
    locator JSONB;
    source_ordinal BIGINT;
    expected_prefix TEXT;
    uuid_pattern CONSTANT TEXT :=
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
BEGIN
    IF NEW.status <> 'completed'
       OR NEW.grounding_profile <> 'certus_atomic_claim_evidence:v1' THEN
        RETURN NEW;
    END IF;

    IF NEW.evidence_manifest ->> 'profile'
            IS DISTINCT FROM 'certus_typed_evidence_manifest:v1'
       OR jsonb_typeof(NEW.evidence_manifest -> 'sources') IS DISTINCT FROM 'array'
       OR COALESCE(NEW.evidence_manifest ->> 'source_count', '') !~ '^[0-9]{1,2}$'
       OR (NEW.evidence_manifest ->> 'source_count')::INTEGER
            <> jsonb_array_length(NEW.evidence_manifest -> 'sources')
       OR jsonb_array_length(NEW.evidence_manifest -> 'sources') > 64
       OR COALESCE(NEW.evidence_manifest ->> 'canonical_sha256', '')
            !~ '^[0-9a-f]{64}$'
       OR COALESCE(NEW.evidence_manifest ->> 'rendered_pack_sha256', '')
            !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'malformed v1 evidence manifest';
    END IF;

    IF (
        SELECT COUNT(*) <> COUNT(DISTINCT item ->> 'evidence_id')
        FROM jsonb_array_elements(NEW.evidence_manifest -> 'sources') AS item
    ) THEN
        RAISE EXCEPTION 'evidence manifest IDs must be unique';
    END IF;

    FOR source, source_ordinal IN
        SELECT value, ordinality
        FROM jsonb_array_elements(NEW.evidence_manifest -> 'sources')
             WITH ORDINALITY AS item(value, ordinality)
    LOOP
        expected_prefix := CASE source ->> 'source_kind'
            WHEN 'document' THEN 'D'
            WHEN 'memory' THEN 'M'
            WHEN 'graph' THEN 'G'
            WHEN 'tool' THEN 'T'
            ELSE NULL
        END;
        IF jsonb_typeof(source) IS DISTINCT FROM 'object'
           OR expected_prefix IS NULL
           OR COALESCE(source ->> 'evidence_id', '')
                !~ ('^' || expected_prefix || '[1-9][0-9]{0,2}$')
           OR COALESCE(source ->> 'ordinal', '') !~ '^[1-9][0-9]{0,2}$'
           OR (source ->> 'ordinal')::BIGINT <> source_ordinal
           OR COALESCE(source ->> 'content_sha256', '') !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION 'malformed v1 evidence manifest source';
        END IF;

        locator := source -> 'locator';
        IF source ->> 'source_kind' = 'document' THEN
            IF jsonb_typeof(locator) IS DISTINCT FROM 'object'
               OR source ? 'content_snapshot'
               OR COALESCE(locator ->> 'chunk_id', '') !~ uuid_pattern
               OR COALESCE(locator ->> 'document_id', '') !~ uuid_pattern
               OR COALESCE(locator ->> 'document_version_id', '') !~ uuid_pattern
               OR COALESCE(locator ->> 'derivation_id', '') !~ uuid_pattern
               OR COALESCE(locator ->> 'parsed_artifact_id', '') !~ uuid_pattern
               OR LENGTH(COALESCE(locator ->> 'document_title', '')) NOT BETWEEN 1 AND 1000
               OR COALESCE(locator ->> 'content_hash', '') !~ '^[0-9a-f]{64}$'
               OR COALESCE(locator ->> 'version_number', '') !~ '^[1-9][0-9]*$' THEN
                RAISE EXCEPTION 'malformed document evidence locator';
            END IF;

            IF NOT EXISTS (
                SELECT 1
                FROM chunks AS chunk
                JOIN document_derivations AS derivation
                  ON derivation.id = chunk.derivation_id
                 AND derivation.document_id = chunk.document_id
                 AND derivation.document_version_id = chunk.document_version_id
                JOIN document_versions AS version
                  ON version.id = chunk.document_version_id
                 AND version.document_id = chunk.document_id
                WHERE chunk.id = (locator ->> 'chunk_id')::UUID
                  AND chunk.document_id = (locator ->> 'document_id')::UUID
                  AND chunk.document_version_id = (locator ->> 'document_version_id')::UUID
                  AND chunk.derivation_id = (locator ->> 'derivation_id')::UUID
                  AND derivation.input_parsed_artifact_id =
                        (locator ->> 'parsed_artifact_id')::UUID
                  AND chunk.tenant_id = NEW.tenant_id
                  AND chunk.user_id = NEW.user_id
                  AND version.tenant_id = NEW.tenant_id
                  AND version.user_id = NEW.user_id
                  AND version.version_number = (locator ->> 'version_number')::INTEGER
                  AND version.title = locator ->> 'document_title'
                  AND version.content_hash = locator ->> 'content_hash'
                  AND encode(
                        digest(convert_to(chunk.content, 'UTF8'), 'sha256'),
                        'hex'
                      ) = source ->> 'content_sha256'
            ) THEN
                RAISE EXCEPTION 'document evidence manifest does not resolve to immutable run evidence';
            END IF;
        ELSE
            IF jsonb_typeof(source -> 'content_snapshot') IS DISTINCT FROM 'string'
               OR LENGTH(source ->> 'content_snapshot') > 50000
               OR encode(
                    digest(convert_to(source ->> 'content_snapshot', 'UTF8'), 'sha256'),
                    'hex'
                  ) IS DISTINCT FROM source ->> 'content_sha256' THEN
                RAISE EXCEPTION 'non-document evidence snapshot is missing or corrupt';
            END IF;
        END IF;
    END LOOP;

    FOR claim IN SELECT value FROM jsonb_array_elements(NEW.claim_evidence)
    LOOP
        FOR source_ref IN SELECT value FROM jsonb_array_elements(claim -> 'source_refs')
        LOOP
            IF NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.evidence_manifest -> 'sources') AS manifest_source
                WHERE manifest_source ->> 'evidence_id' = source_ref ->> 'source_id'
                  AND manifest_source ->> 'source_kind' = source_ref ->> 'source_kind'
                  AND manifest_source ->> 'content_sha256' = source_ref ->> 'content_sha256'
            ) THEN
                RAISE EXCEPTION 'claim source is absent from the frozen evidence manifest';
            END IF;
        END LOOP;
    END LOOP;

    FOR citation IN SELECT value FROM jsonb_array_elements(NEW.citations)
    LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements(NEW.evidence_manifest -> 'sources') AS manifest_source
            WHERE manifest_source ->> 'evidence_id' = citation ->> 'evidence_id'
              AND manifest_source ->> 'source_kind' = 'document'
              AND manifest_source ->> 'content_sha256' = citation ->> 'quote_sha256'
              AND manifest_source #>> '{locator,chunk_id}' = citation ->> 'chunk_id'
              AND manifest_source #>> '{locator,document_version_id}' =
                    citation ->> 'document_version_id'
              AND manifest_source #>> '{locator,derivation_id}' = citation ->> 'derivation_id'
              AND manifest_source #>> '{locator,parsed_artifact_id}' =
                    citation ->> 'parsed_artifact_id'
        ) THEN
            RAISE EXCEPTION 'selected citation is absent from the frozen document evidence manifest';
        END IF;
    END LOOP;

    IF NEW.generation_profile ->> 'profile'
            IS DISTINCT FROM 'certus_grounded_generation:v1'
       OR NEW.generation_profile ->> 'execution_mode' NOT IN (
            'openai_structured', 'local_extractive',
            'local_extractive_fallback', 'local_action'
       )
       OR NEW.generation_profile ->> 'provider' NOT IN ('openai', 'certus_local')
       OR COALESCE(NEW.generation_profile ->> 'requested_model', '') = ''
       OR COALESCE(NEW.generation_profile ->> 'prompt_profile', '')
            <> 'certus_atomic_claim_prompt:v1'
       OR COALESCE(NEW.generation_profile ->> 'validator_profile', '')
            <> 'certus_mechanical_claim_validator:v1'
       OR COALESCE(NEW.generation_profile ->> 'grounding_profile', '')
            <> 'certus_atomic_claim_evidence:v1'
       OR COALESCE(NEW.generation_profile ->> 'schema_name', '')
            <> 'certus_atomic_claim_answer'
       OR COALESCE(NEW.generation_profile ->> 'instructions_sha256', '')
            !~ '^[0-9a-f]{64}$'
       OR COALESCE(NEW.generation_profile ->> 'input_sha256', '')
            !~ '^[0-9a-f]{64}$'
       OR COALESCE(NEW.generation_profile ->> 'schema_sha256', '')
            !~ '^[0-9a-f]{64}$'
       OR COALESCE(NEW.generation_profile ->> 'canonical_sha256', '')
            !~ '^[0-9a-f]{64}$'
       OR NEW.generation_profile ->> 'evidence_manifest_sha256'
            IS DISTINCT FROM NEW.evidence_manifest ->> 'canonical_sha256'
       OR NEW.generation_profile ->> 'store' IS DISTINCT FROM 'false' THEN
        RAISE EXCEPTION 'malformed v1 generation profile';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_validate_replayable_agent_run_provenance ON agent_runs;
CREATE CONSTRAINT TRIGGER trg_validate_replayable_agent_run_provenance
    AFTER INSERT OR UPDATE OF status, grounding_profile, claim_evidence,
        citations, evidence_manifest, generation_profile
    ON agent_runs
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_replayable_agent_run_provenance();

COMMENT ON COLUMN agent_runs.evidence_manifest IS
    'Complete ordered typed generation evidence: immutable document locators plus bounded non-document snapshots.';
COMMENT ON COLUMN agent_runs.generation_profile IS
    'Credential-free request, provider response, prompt/schema, validator, and fallback provenance for exact audit/replay.';
COMMENT ON COLUMN agent_runs.replay_mode IS
    'Original execution, exact frozen-evidence replay, or explicitly fresh retrieval rerun.';
COMMENT ON COLUMN agent_runs.evidence_manifest_db_sha256 IS
    'Database-canonical SHA-256 fingerprint of the stored evidence manifest JSONB.';
COMMENT ON COLUMN agent_runs.generation_profile_db_sha256 IS
    'Database-canonical SHA-256 fingerprint of the stored generation profile JSONB.';
