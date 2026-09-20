-- Migration 068 deliberately preserves only already-completed legacy proof.
-- Treat historical NULL statuses as open/invalid rather than letting SQL's
-- three-valued comparison place them in the legacy compatibility set.

UPDATE agent_runs
SET citation_span_contract_version = 1
WHERE status IS NULL
  AND citation_span_contract_version = 0;

CREATE OR REPLACE FUNCTION require_current_citation_span_contract()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status <> 'completed'
       OR NEW.grounding_profile <> 'certus_atomic_claim_evidence:v1' THEN
        RETURN NEW;
    END IF;

    IF NEW.citation_span_contract_version = 0 THEN
        IF TG_OP = 'INSERT' THEN
            RAISE EXCEPTION 'newly completed v1 answer requires citation-span contract v1';
        ELSIF OLD.status IS DISTINCT FROM 'completed' THEN
            RAISE EXCEPTION 'newly completed v1 answer requires citation-span contract v1';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.citation_span_contract_version <> 1
       OR EXISTS (
            SELECT 1
            FROM jsonb_array_elements(NEW.citations) AS citation
            WHERE citation ->> 'claim_span_profile' IS DISTINCT FROM
                'certus_claim_aligned_sentence_span:unicode_code_point:v1'
       ) THEN
        RAISE EXCEPTION 'completed v1 answer is missing current claim-aligned citation spans';
    END IF;

    RETURN NEW;
END;
$$;
