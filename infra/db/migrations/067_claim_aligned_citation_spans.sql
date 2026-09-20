-- Add claim-specific, immutable parsed-text subspans without rewriting legacy
-- v1 runs. The selected sentence window is deterministic application logic;
-- this database contract proves that every stored window is an exact substring
-- of its tenant-owned chunk and is hash-bound to the corresponding claim ref.

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
       ) THEN
        RAISE EXCEPTION 'completed agent-run answer evidence is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_protect_completed_agent_run_answer_evidence ON agent_runs;
CREATE TRIGGER trg_protect_completed_agent_run_answer_evidence
    BEFORE UPDATE OF output_response, citations, claim_evidence,
        answer_status, grounding_profile
    ON agent_runs
    FOR EACH ROW EXECUTE FUNCTION protect_completed_agent_run_answer_evidence();

CREATE OR REPLACE FUNCTION validate_claim_aligned_citation_spans()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    citation JSONB;
    claim_span JSONB;
    chunk_content TEXT;
    chunk_start INTEGER;
    chunk_end INTEGER;
    chunk_locator_status TEXT;
    relative_start INTEGER;
    relative_end INTEGER;
    absolute_start INTEGER;
    absolute_end INTEGER;
BEGIN
    IF NEW.status <> 'completed'
       OR NEW.grounding_profile <> 'certus_atomic_claim_evidence:v1' THEN
        RETURN NEW;
    END IF;

    FOR citation IN SELECT value FROM jsonb_array_elements(NEW.citations)
    LOOP
        IF NOT citation ? 'claim_span_profile' THEN
            CONTINUE;
        END IF;
        IF citation ->> 'claim_span_profile'
                IS DISTINCT FROM 'certus_claim_aligned_sentence_span:unicode_code_point:v1'
           OR jsonb_typeof(citation -> 'claim_spans') IS DISTINCT FROM 'array'
           OR jsonb_array_length(citation -> 'claim_spans') = 0
           OR jsonb_array_length(citation -> 'claim_spans')
                <> jsonb_array_length(citation -> 'claim_ids') THEN
            RAISE EXCEPTION 'malformed claim-aligned citation span collection';
        END IF;

        IF (
            SELECT COUNT(*) <> COUNT(DISTINCT item ->> 'claim_id')
            FROM jsonb_array_elements(citation -> 'claim_spans') AS item
        ) OR EXISTS (
            SELECT 1
            FROM (
                (SELECT jsonb_array_elements_text(citation -> 'claim_ids') AS claim_id
                 EXCEPT
                 SELECT item ->> 'claim_id'
                 FROM jsonb_array_elements(citation -> 'claim_spans') AS item)
                UNION ALL
                (SELECT item ->> 'claim_id'
                 FROM jsonb_array_elements(citation -> 'claim_spans') AS item
                 EXCEPT
                 SELECT jsonb_array_elements_text(citation -> 'claim_ids'))
            ) AS difference
        ) THEN
            RAISE EXCEPTION 'claim-aligned spans must map one-to-one to citation claims';
        END IF;

        SELECT chunk.content, chunk.start_char, chunk.end_char,
               chunk.text_locator_status
        INTO chunk_content, chunk_start, chunk_end, chunk_locator_status
        FROM chunks AS chunk
        WHERE chunk.id = (citation ->> 'chunk_id')::UUID
          AND chunk.tenant_id = NEW.tenant_id
          AND chunk.user_id = NEW.user_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'claim-aligned citation chunk is unavailable or unauthorized';
        END IF;

        FOR claim_span IN
            SELECT value FROM jsonb_array_elements(citation -> 'claim_spans')
        LOOP
            IF jsonb_typeof(claim_span) IS DISTINCT FROM 'object'
               OR claim_span ->> 'profile' IS DISTINCT FROM
                    'certus_claim_aligned_sentence_span:unicode_code_point:v1'
               OR claim_span ->> 'selection_status' NOT IN (
                    'claim_aligned', 'full_chunk_fallback'
               )
               OR COALESCE(claim_span ->> 'claim_id', '') !~ '^C[1-9][0-9]{0,2}$'
               OR LENGTH(COALESCE(claim_span ->> 'quote', '')) = 0
               OR COALESCE(claim_span ->> 'quote_sha256', '') !~ '^[0-9a-f]{64}$'
               OR COALESCE(claim_span ->> 'relative_start_char', '') !~ '^[0-9]+$'
               OR COALESCE(claim_span ->> 'relative_end_char', '') !~ '^[1-9][0-9]*$'
               OR claim_span ->> 'semantic_entailment_checked' IS DISTINCT FROM 'false' THEN
                RAISE EXCEPTION 'malformed claim-aligned citation span';
            END IF;

            relative_start := (claim_span ->> 'relative_start_char')::INTEGER;
            relative_end := (claim_span ->> 'relative_end_char')::INTEGER;
            IF relative_end <= relative_start
               OR relative_end > CHAR_LENGTH(chunk_content)
               OR SUBSTRING(
                    chunk_content FROM relative_start + 1
                    FOR relative_end - relative_start
                  ) IS DISTINCT FROM claim_span ->> 'quote'
               OR encode(
                    digest(convert_to(claim_span ->> 'quote', 'UTF8'), 'sha256'),
                    'hex'
                  ) IS DISTINCT FROM claim_span ->> 'quote_sha256' THEN
                RAISE EXCEPTION 'claim-aligned citation span does not resolve to its chunk';
            END IF;

            IF claim_span ->> 'selection_status' = 'full_chunk_fallback'
               AND (relative_start <> 0 OR relative_end <> CHAR_LENGTH(chunk_content)) THEN
                RAISE EXCEPTION 'full-chunk citation fallback must retain the complete chunk';
            ELSIF claim_span ->> 'selection_status' = 'claim_aligned'
               AND relative_start = 0 AND relative_end = CHAR_LENGTH(chunk_content) THEN
                RAISE EXCEPTION 'claim-aligned citation span must be narrower than its chunk';
            END IF;

            IF chunk_locator_status = 'exact' THEN
                IF claim_span ->> 'text_locator_status' IS DISTINCT FROM 'exact'
                   OR COALESCE(claim_span ->> 'start_char', '') !~ '^[0-9]+$'
                   OR COALESCE(claim_span ->> 'end_char', '') !~ '^[1-9][0-9]*$' THEN
                    RAISE EXCEPTION 'exact claim span has malformed parsed-artifact offsets';
                END IF;
                absolute_start := (claim_span ->> 'start_char')::INTEGER;
                absolute_end := (claim_span ->> 'end_char')::INTEGER;
                IF chunk_start IS NULL OR chunk_end IS NULL
                   OR chunk_end - chunk_start <> CHAR_LENGTH(chunk_content)
                   OR absolute_start <> chunk_start + relative_start
                   OR absolute_end <> chunk_start + relative_end THEN
                    RAISE EXCEPTION 'claim span offsets do not resolve to the parsed artifact';
                END IF;
            ELSIF chunk_locator_status = 'unavailable' THEN
                IF claim_span ->> 'text_locator_status' IS DISTINCT FROM 'unavailable'
                   OR claim_span ->> 'start_char' IS NOT NULL
                   OR claim_span ->> 'end_char' IS NOT NULL THEN
                    RAISE EXCEPTION 'unavailable claim span cannot assert parsed-artifact offsets';
                END IF;
            ELSE
                RAISE EXCEPTION 'claim span references an unknown chunk locator status';
            END IF;

            IF NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.claim_evidence) AS claim
                CROSS JOIN LATERAL jsonb_array_elements(claim -> 'source_refs') AS source_ref
                WHERE claim ->> 'claim_id' = claim_span ->> 'claim_id'
                  AND source_ref ->> 'source_kind' = 'document'
                  AND source_ref ->> 'source_id' = citation ->> 'evidence_id'
                  AND source_ref ->> 'claim_span_sha256' = claim_span ->> 'quote_sha256'
            ) THEN
                RAISE EXCEPTION 'claim span hash is not bound to its document source reference';
            END IF;
        END LOOP;
    END LOOP;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_validate_claim_aligned_citation_spans ON agent_runs;
CREATE CONSTRAINT TRIGGER trg_validate_claim_aligned_citation_spans
    AFTER INSERT OR UPDATE OF status, grounding_profile, claim_evidence, citations
    ON agent_runs
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_claim_aligned_citation_spans();

COMMENT ON COLUMN agent_runs.citations IS
    'Selected document evidence; newer v1 runs include exact per-claim sentence spans hash-bound to immutable chunks.';
