-- Close the remaining structural gap in migration 035: a selected document
-- citation must carry the same exact quote digest as the claim source reference.
-- This does not assert semantic entailment.

CREATE OR REPLACE FUNCTION validate_claim_citation_hash_binding()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    claim JSONB;
    source_ref JSONB;
    matching_citation JSONB;
BEGIN
    IF NEW.status <> 'completed'
       OR NEW.grounding_profile <> 'certus_atomic_claim_evidence:v1' THEN
        RETURN NEW;
    END IF;

    FOR claim IN SELECT value FROM jsonb_array_elements(NEW.claim_evidence)
    LOOP
        FOR source_ref IN SELECT value FROM jsonb_array_elements(claim -> 'source_refs')
        LOOP
            IF source_ref ->> 'source_kind' <> 'document' THEN
                CONTINUE;
            END IF;
            SELECT value INTO matching_citation
            FROM jsonb_array_elements(NEW.citations)
            WHERE value ->> 'evidence_id' = source_ref ->> 'source_id';

            IF matching_citation IS NULL
               OR COALESCE(matching_citation ->> 'quote_sha256', '') !~ '^[0-9a-f]{64}$'
               OR matching_citation ->> 'quote_sha256'
                    IS DISTINCT FROM source_ref ->> 'content_sha256' THEN
                RAISE EXCEPTION 'document claim source hash does not match its selected citation quote';
            END IF;
        END LOOP;
    END LOOP;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM agent_runs AS run
        CROSS JOIN LATERAL jsonb_array_elements(run.claim_evidence) AS claim(value)
        CROSS JOIN LATERAL jsonb_array_elements(claim.value -> 'source_refs') AS source_ref(value)
        LEFT JOIN LATERAL (
            SELECT citation.value AS citation
            FROM jsonb_array_elements(run.citations) AS citation(value)
            WHERE citation.value ->> 'evidence_id' = source_ref.value ->> 'source_id'
            LIMIT 1
        ) AS selected ON TRUE
        WHERE run.status = 'completed'
          AND run.grounding_profile = 'certus_atomic_claim_evidence:v1'
          AND source_ref.value ->> 'source_kind' = 'document'
          AND (
              selected.citation IS NULL
              OR COALESCE(selected.citation ->> 'quote_sha256', '') !~ '^[0-9a-f]{64}$'
              OR selected.citation ->> 'quote_sha256'
                   IS DISTINCT FROM source_ref.value ->> 'content_sha256'
          )
    ) THEN
        RAISE EXCEPTION 'existing v1 claim/citation hash binding is invalid';
    END IF;
END;
$$;

DROP TRIGGER IF EXISTS trg_validate_claim_citation_hash_binding ON agent_runs;
CREATE CONSTRAINT TRIGGER trg_validate_claim_citation_hash_binding
    AFTER INSERT OR UPDATE OF status, grounding_profile, claim_evidence, citations
    ON agent_runs
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_claim_citation_hash_binding();
