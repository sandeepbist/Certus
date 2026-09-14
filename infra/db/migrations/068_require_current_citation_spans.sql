-- Distinguish immutable legacy completed answers from runs that must satisfy
-- the claim-span contract introduced by migration 067. Existing completed
-- proof records remain byte-for-byte unchanged; every open or future run is
-- required to persist the current contract before it can complete.

ALTER TABLE agent_runs
    ADD COLUMN citation_span_contract_version SMALLINT NOT NULL DEFAULT 0
    CHECK (citation_span_contract_version IN (0, 1));

UPDATE agent_runs
SET citation_span_contract_version = 1
WHERE status <> 'completed';

ALTER TABLE agent_runs
    ALTER COLUMN citation_span_contract_version SET DEFAULT 1;

CREATE OR REPLACE FUNCTION protect_completed_agent_run_answer_evidence()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status = 'completed'
       AND (
            OLD.output_response IS DISTINCT FROM NEW.output_response
            OR OLD.citations IS DISTINCT FROM NEW.citations
            OR OLD.claim_evidence IS DISTINCT FROM NEW.claim_evidence
            OR OLD.answer_status IS DISTINCT FROM NEW.answer_status
            OR OLD.grounding_profile IS DISTINCT FROM NEW.grounding_profile
            OR OLD.citation_span_contract_version IS DISTINCT FROM
                NEW.citation_span_contract_version
       ) THEN
        RAISE EXCEPTION 'completed agent-run answer evidence is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_protect_completed_agent_run_answer_evidence ON agent_runs;
CREATE TRIGGER trg_protect_completed_agent_run_answer_evidence
    BEFORE UPDATE OF output_response, citations, claim_evidence,
        answer_status, grounding_profile, citation_span_contract_version
    ON agent_runs
    FOR EACH ROW EXECUTE FUNCTION protect_completed_agent_run_answer_evidence();

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
        ELSIF OLD.status <> 'completed' THEN
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

DROP TRIGGER IF EXISTS trg_require_current_citation_span_contract ON agent_runs;
CREATE CONSTRAINT TRIGGER trg_require_current_citation_span_contract
    AFTER INSERT OR UPDATE OF status, grounding_profile, citations,
        citation_span_contract_version
    ON agent_runs
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION require_current_citation_span_contract();

COMMENT ON COLUMN agent_runs.citation_span_contract_version IS
    '0 only for completed pre-cutover evidence; 1 requires per-claim citation spans for v1 document citations.';
