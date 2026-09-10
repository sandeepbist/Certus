-- Replace synthetic chunk counters with database-verified half-open character
-- spans into the immutable parsed artifact. Existing offsets were produced by
-- whitespace-normalizing chunkers and cannot be trusted, so preserve those
-- chunks while marking their text locator explicitly unavailable.

ALTER TABLE chunks
    ADD COLUMN text_locator_status VARCHAR(20),
    ADD COLUMN text_locator_profile VARCHAR(80),
    ADD COLUMN text_locator_unavailable_reason TEXT;

DROP TRIGGER trg_protect_chunk_provenance ON chunks;

UPDATE chunks
SET start_char = NULL,
    end_char = NULL,
    text_locator_status = 'unavailable',
    text_locator_profile = 'legacy_unavailable:v0',
    text_locator_unavailable_reason =
        'Chunk offsets created before migration 033 were not exact parsed-artifact positions.';

ALTER TABLE chunks
    ALTER COLUMN text_locator_status SET NOT NULL,
    ALTER COLUMN text_locator_profile SET NOT NULL,
    ADD CONSTRAINT chunks_text_locator_state_check CHECK (
        (
            text_locator_status = 'exact'
            AND text_locator_profile = 'unicode_code_point:zero_based_half_open:v1'
            AND start_char IS NOT NULL
            AND end_char IS NOT NULL
            AND start_char >= 0
            AND end_char > start_char
            AND text_locator_unavailable_reason IS NULL
        )
        OR (
            text_locator_status = 'unavailable'
            AND text_locator_profile = 'legacy_unavailable:v0'
            AND start_char IS NULL
            AND end_char IS NULL
            AND text_locator_unavailable_reason IS NOT NULL
        )
    );

CREATE FUNCTION validate_new_chunk_text_locators()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM new_chunks
        WHERE text_locator_status <> 'exact'
    ) THEN
        RAISE EXCEPTION 'new chunks require an exact parsed-artifact text locator';
    END IF;

    IF EXISTS (
        WITH parsed_inputs AS MATERIALIZED (
            SELECT derivation.id AS derivation_id,
                   derivation.document_version_id,
                   derivation.document_id,
                   derivation.tenant_id,
                   derivation.user_id,
                   parsed.status,
                   parsed.content_text,
                   char_length(parsed.content_text) AS content_length
            FROM document_derivations AS derivation
            JOIN document_parsed_artifacts AS parsed
              ON parsed.id = derivation.input_parsed_artifact_id
             AND parsed.document_version_id = derivation.document_version_id
             AND parsed.document_id = derivation.document_id
             AND parsed.tenant_id = derivation.tenant_id
             AND parsed.user_id = derivation.user_id
            WHERE EXISTS (
                SELECT 1
                FROM new_chunks AS candidate
                WHERE candidate.derivation_id = derivation.id
                  AND candidate.document_version_id = derivation.document_version_id
                  AND candidate.document_id = derivation.document_id
                  AND candidate.tenant_id = derivation.tenant_id
                  AND candidate.user_id = derivation.user_id
            )
        )
        SELECT 1
        FROM new_chunks AS candidate
        LEFT JOIN parsed_inputs AS parsed
          ON parsed.derivation_id = candidate.derivation_id
         AND parsed.document_version_id = candidate.document_version_id
         AND parsed.document_id = candidate.document_id
         AND parsed.tenant_id = candidate.tenant_id
         AND parsed.user_id = candidate.user_id
        WHERE parsed.derivation_id IS NULL
           OR parsed.status <> 'ready'
           OR candidate.end_char > parsed.content_length
           OR substring(
                parsed.content_text
                FROM candidate.start_char + 1
                FOR candidate.end_char - candidate.start_char
              ) IS DISTINCT FROM candidate.content
    ) THEN
        RAISE EXCEPTION 'new chunk content does not match its exact parsed-artifact span';
    END IF;

    RETURN NULL;
END;
$$;

CREATE TRIGGER trg_validate_new_chunk_text_locators
    AFTER INSERT ON chunks
    REFERENCING NEW TABLE AS new_chunks
    FOR EACH STATEMENT EXECUTE FUNCTION validate_new_chunk_text_locators();

CREATE OR REPLACE FUNCTION protect_chunk_provenance()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.id IS DISTINCT FROM NEW.id
       OR OLD.document_id IS DISTINCT FROM NEW.document_id
       OR OLD.document_version_id IS DISTINCT FROM NEW.document_version_id
       OR OLD.derivation_id IS DISTINCT FROM NEW.derivation_id
       OR OLD.user_id IS DISTINCT FROM NEW.user_id
       OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.content IS DISTINCT FROM NEW.content
       OR OLD.contextualized_content IS DISTINCT FROM NEW.contextualized_content
       OR OLD.chunk_index IS DISTINCT FROM NEW.chunk_index
       OR OLD.token_count IS DISTINCT FROM NEW.token_count
       OR OLD.start_char IS DISTINCT FROM NEW.start_char
       OR OLD.end_char IS DISTINCT FROM NEW.end_char
       OR OLD.page_number IS DISTINCT FROM NEW.page_number
       OR OLD.section_title IS DISTINCT FROM NEW.section_title
       OR OLD.language IS DISTINCT FROM NEW.language
       OR OLD.processing_generation IS DISTINCT FROM NEW.processing_generation
       OR OLD.embedding_profile IS DISTINCT FROM NEW.embedding_profile
       OR OLD.text_locator_status IS DISTINCT FROM NEW.text_locator_status
       OR OLD.text_locator_profile IS DISTINCT FROM NEW.text_locator_profile
       OR OLD.text_locator_unavailable_reason IS DISTINCT FROM NEW.text_locator_unavailable_reason
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'chunk source and provenance fields are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_protect_chunk_provenance
    BEFORE UPDATE ON chunks
    FOR EACH ROW EXECUTE FUNCTION protect_chunk_provenance();

COMMENT ON COLUMN chunks.start_char IS
    'Zero-based inclusive Unicode-character offset into the derivation input parsed artifact; meaningful only when text_locator_status is exact.';
COMMENT ON COLUMN chunks.end_char IS
    'Zero-based exclusive Unicode-character offset into the derivation input parsed artifact; meaningful only when text_locator_status is exact.';
COMMENT ON COLUMN chunks.text_locator_status IS
    'Whether start_char/end_char are database-verified exact parsed-artifact positions or explicitly unavailable legacy provenance.';
COMMENT ON COLUMN chunks.text_locator_profile IS
    'Versioned offset unit and range semantics. Exact v1 locators use Unicode code points and zero-based half-open ranges.';
