-- Persist the exact server-owned claim/source contract for each completed chat
-- run. Learned entailment is deliberately not asserted by this migration:
-- v1 proves structural integrity and deterministic exact-value checks only.

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS answer_status TEXT NOT NULL DEFAULT 'legacy_unavailable',
    ADD COLUMN IF NOT EXISTS grounding_profile TEXT NOT NULL DEFAULT 'legacy_unavailable:v0',
    ADD COLUMN IF NOT EXISTS claim_evidence JSONB NOT NULL DEFAULT '[]'::jsonb;

UPDATE agent_runs
SET citations = '[]'::jsonb
WHERE citations IS NULL;

ALTER TABLE agent_runs
    ALTER COLUMN citations SET DEFAULT '[]'::jsonb,
    ALTER COLUMN citations SET NOT NULL;

ALTER TABLE agent_runs
    DROP CONSTRAINT IF EXISTS agent_runs_answer_status_check,
    DROP CONSTRAINT IF EXISTS agent_runs_grounding_profile_check,
    DROP CONSTRAINT IF EXISTS agent_runs_claim_evidence_shape_check,
    DROP CONSTRAINT IF EXISTS agent_runs_citations_shape_check;

ALTER TABLE agent_runs
    ADD CONSTRAINT agent_runs_answer_status_check CHECK (
        answer_status IN (
            'legacy_unavailable', 'pending', 'answered', 'extractive',
            'insufficient_evidence', 'conflicting_evidence', 'action_completed'
        )
    ),
    ADD CONSTRAINT agent_runs_grounding_profile_check CHECK (
        grounding_profile IN (
            'legacy_unavailable:v0', 'certus_atomic_claim_evidence:v1'
        )
    ),
    ADD CONSTRAINT agent_runs_claim_evidence_shape_check CHECK (
        jsonb_typeof(claim_evidence) = 'array'
        AND jsonb_array_length(claim_evidence) <= 24
    ),
    ADD CONSTRAINT agent_runs_citations_shape_check CHECK (
        jsonb_typeof(citations) = 'array'
        AND jsonb_array_length(citations) <= 24
    );

CREATE OR REPLACE FUNCTION validate_atomic_claim_evidence()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    claim JSONB;
    source_ref JSONB;
    citation JSONB;
    cited_claim_id TEXT;
    expected_prefix TEXT;
BEGIN
    IF NEW.status <> 'completed'
       OR NEW.grounding_profile <> 'certus_atomic_claim_evidence:v1' THEN
        RETURN NEW;
    END IF;

    IF NEW.answer_status IN ('answered', 'extractive', 'conflicting_evidence')
       AND jsonb_array_length(NEW.claim_evidence) = 0 THEN
        RAISE EXCEPTION 'substantive grounded answer requires at least one claim';
    END IF;
    IF NEW.answer_status IN ('insufficient_evidence', 'action_completed')
       AND jsonb_array_length(NEW.claim_evidence) <> 0 THEN
        RAISE EXCEPTION 'non-claim answer status cannot retain factual claims';
    END IF;

    IF (
        SELECT COUNT(*) <> COUNT(DISTINCT item ->> 'claim_id')
        FROM jsonb_array_elements(NEW.claim_evidence) AS item
    ) THEN
        RAISE EXCEPTION 'claim IDs must be unique within an agent run';
    END IF;
    IF (
        SELECT COUNT(*) <> COUNT(DISTINCT item ->> 'evidence_id')
        FROM jsonb_array_elements(NEW.citations) AS item
    ) THEN
        RAISE EXCEPTION 'citation evidence IDs must be unique within an agent run';
    END IF;

    FOR claim IN SELECT value FROM jsonb_array_elements(NEW.claim_evidence)
    LOOP
        IF jsonb_typeof(claim) IS DISTINCT FROM 'object'
           OR COALESCE(claim ->> 'claim_id', '') !~ '^C[1-9][0-9]{0,2}$'
           OR LENGTH(COALESCE(claim ->> 'text', '')) NOT BETWEEN 1 AND 4000
           OR jsonb_typeof(claim -> 'source_refs') IS DISTINCT FROM 'array'
           OR jsonb_array_length(claim -> 'source_refs') NOT BETWEEN 1 AND 5
           OR claim #>> '{mechanical_validation,status}' IS DISTINCT FROM 'passed'
           OR claim ->> 'semantic_support_status' IS DISTINCT FROM 'not_evaluated' THEN
            RAISE EXCEPTION 'malformed v1 atomic claim evidence';
        END IF;

        IF (
            SELECT COUNT(*) <> COUNT(DISTINCT item ->> 'source_id')
            FROM jsonb_array_elements(claim -> 'source_refs') AS item
        ) THEN
            RAISE EXCEPTION 'a claim cannot repeat a source reference';
        END IF;

        FOR source_ref IN SELECT value FROM jsonb_array_elements(claim -> 'source_refs')
        LOOP
            expected_prefix := CASE source_ref ->> 'source_kind'
                WHEN 'document' THEN 'D'
                WHEN 'memory' THEN 'M'
                WHEN 'graph' THEN 'G'
                WHEN 'tool' THEN 'T'
                ELSE NULL
            END;
            IF expected_prefix IS NULL
               OR COALESCE(source_ref ->> 'source_id', '') !~ ('^' || expected_prefix || '[1-9][0-9]{0,2}$')
               OR COALESCE(source_ref ->> 'content_sha256', '') !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'malformed v1 claim source reference';
            END IF;
            IF source_ref ->> 'source_kind' = 'document'
               AND NOT EXISTS (
                   SELECT 1
                   FROM jsonb_array_elements(NEW.citations) AS item
                   WHERE item ->> 'evidence_id' = source_ref ->> 'source_id'
                     AND item -> 'claim_ids' ? (claim ->> 'claim_id')
               ) THEN
                RAISE EXCEPTION 'document claim source is not linked by a selected citation';
            END IF;
        END LOOP;
    END LOOP;

    FOR citation IN SELECT value FROM jsonb_array_elements(NEW.citations)
    LOOP
        IF jsonb_typeof(citation) IS DISTINCT FROM 'object'
           OR COALESCE(citation ->> 'evidence_id', '') !~ '^D[1-9][0-9]{0,2}$'
           OR citation ->> 'support_scope' IS DISTINCT FROM 'atomic_claim_selected'
           OR citation ->> 'verification_status' IS DISTINCT FROM 'mechanical_checks_passed_semantic_not_evaluated'
           OR jsonb_typeof(citation -> 'claim_ids') IS DISTINCT FROM 'array'
           OR jsonb_array_length(citation -> 'claim_ids') = 0 THEN
            RAISE EXCEPTION 'malformed v1 selected citation';
        END IF;
        FOR cited_claim_id IN SELECT jsonb_array_elements_text(citation -> 'claim_ids')
        LOOP
            IF NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.claim_evidence) AS item
                CROSS JOIN LATERAL jsonb_array_elements(item -> 'source_refs') AS ref
                WHERE item ->> 'claim_id' = cited_claim_id
                  AND ref ->> 'source_kind' = 'document'
                  AND ref ->> 'source_id' = citation ->> 'evidence_id'
            ) THEN
                RAISE EXCEPTION 'citation links to a nonexistent claim source';
            END IF;
        END LOOP;
    END LOOP;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_validate_atomic_claim_evidence ON agent_runs;
CREATE CONSTRAINT TRIGGER trg_validate_atomic_claim_evidence
    AFTER INSERT OR UPDATE OF status, answer_status, grounding_profile, claim_evidence, citations
    ON agent_runs
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_atomic_claim_evidence();

COMMENT ON COLUMN agent_runs.claim_evidence IS
    'Server-assigned atomic claims and typed source references; v1 mechanical validation is not semantic entailment.';
COMMENT ON COLUMN agent_runs.answer_status IS
    'Truthful answer disposition: answered/extractive/abstained/conflicting/action, with legacy state explicit.';
