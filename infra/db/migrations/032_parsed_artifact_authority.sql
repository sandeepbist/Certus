-- Complete the expand/contract rollout begun in migration 031. Every source
-- version must now have one explicit original-object record and one immutable
-- parsed artifact, and every derivation must name the parsed artifact it used.

-- Cover versions written by an older process during the migration 031 rolling
-- window. Their original bytes cannot be reconstructed, so record that fact
-- rather than inventing a storage locator.
INSERT INTO document_source_objects (
    id, document_version_id, document_id, tenant_id, user_id,
    storage_backend, original_filename, claimed_mime_type,
    byte_length, content_sha256, checksum_sha256_base64,
    status, unavailable_reason, created_at, updated_at
)
SELECT
    uuid_generate_v5(uuid_ns_url(), 'certus:source-object:' || version.id::text),
    version.id,
    version.document_id,
    version.tenant_id,
    version.user_id,
    'unavailable',
    COALESCE(NULLIF(version.title, ''), 'legacy-document-' || version.version_number::text),
    version.mime_type,
    COALESCE(version.file_size_bytes, 0),
    version.content_hash,
    encode(decode(version.content_hash, 'hex'), 'base64'),
    'unavailable',
    'Original bytes were not retained before the parsed-artifact authority cutover.',
    COALESCE(version.created_at, version.recorded_at),
    COALESCE(version.updated_at, version.created_at, version.recorded_at)
FROM document_versions AS version
LEFT JOIN document_source_objects AS source
  ON source.document_version_id = version.id
WHERE source.id IS NULL
ON CONFLICT (document_version_id) DO NOTHING;

INSERT INTO document_parsed_artifacts (
    id, source_object_id, document_version_id, document_id, tenant_id,
    user_id, content_text, content_sha256, byte_length, producer_profile,
    status, unavailable_reason, created_at
)
SELECT
    uuid_generate_v5(uuid_ns_url(), 'certus:parsed-artifact:' || version.id::text),
    source.id,
    version.id,
    version.document_id,
    version.tenant_id,
    version.user_id,
    version.raw_text,
    CASE
        WHEN version.raw_text IS NULL THEN NULL
        ELSE encode(digest(convert_to(version.raw_text, 'UTF8'), 'sha256'), 'hex')
    END,
    CASE
        WHEN version.raw_text IS NULL THEN NULL
        ELSE octet_length(convert_to(version.raw_text, 'UTF8'))
    END,
    version.parser_profile,
    CASE WHEN version.raw_text IS NULL THEN 'unavailable' ELSE 'ready' END,
    CASE
        WHEN version.raw_text IS NULL
            THEN 'No parsed text existed when artifact authority was established.'
        ELSE NULL
    END,
    COALESCE(version.created_at, version.recorded_at)
FROM document_versions AS version
JOIN document_source_objects AS source
  ON source.document_version_id = version.id
LEFT JOIN document_parsed_artifacts AS parsed
  ON parsed.document_version_id = version.id
WHERE parsed.id IS NULL
ON CONFLICT (document_version_id) DO NOTHING;

UPDATE document_derivations AS derivation
SET input_parsed_artifact_id = parsed.id
FROM document_parsed_artifacts AS parsed
WHERE parsed.document_version_id = derivation.document_version_id
  AND derivation.input_parsed_artifact_id IS NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM document_versions AS version
        LEFT JOIN document_source_objects AS source
          ON source.document_version_id = version.id
        LEFT JOIN document_parsed_artifacts AS parsed
          ON parsed.document_version_id = version.id
        WHERE source.id IS NULL OR parsed.id IS NULL
    ) THEN
        RAISE EXCEPTION 'cannot contract legacy projections: incomplete source lineage';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM document_derivations
        WHERE input_parsed_artifact_id IS NULL
    ) THEN
        RAISE EXCEPTION 'cannot contract legacy projections: unlinked derivations remain';
    END IF;
END;
$$;

ALTER TABLE document_derivations
    ALTER COLUMN input_parsed_artifact_id SET NOT NULL;

DROP TRIGGER trg_protect_document_version_source ON document_versions;
DROP FUNCTION protect_document_version_source();

ALTER TABLE document_versions DROP COLUMN raw_text;
ALTER TABLE documents DROP COLUMN raw_text;

CREATE FUNCTION protect_document_version_source()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.id IS DISTINCT FROM NEW.id
       OR OLD.document_id IS DISTINCT FROM NEW.document_id
       OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.user_id IS DISTINCT FROM NEW.user_id
       OR OLD.version_number IS DISTINCT FROM NEW.version_number
       OR OLD.title IS DISTINCT FROM NEW.title
       OR OLD.source_type IS DISTINCT FROM NEW.source_type
       OR OLD.mime_type IS DISTINCT FROM NEW.mime_type
       OR OLD.file_size_bytes IS DISTINCT FROM NEW.file_size_bytes
       OR OLD.content_hash IS DISTINCT FROM NEW.content_hash
       OR OLD.tags IS DISTINCT FROM NEW.tags
       OR OLD.source_time IS DISTINCT FROM NEW.source_time
       OR OLD.source_time_origin IS DISTINCT FROM NEW.source_time_origin
       OR OLD.recorded_at IS DISTINCT FROM NEW.recorded_at
       OR OLD.parser_profile IS DISTINCT FROM NEW.parser_profile
       OR OLD.source_metadata IS DISTINCT FROM NEW.source_metadata
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'document version source fields are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_protect_document_version_source
    BEFORE UPDATE ON document_versions
    FOR EACH ROW EXECUTE FUNCTION protect_document_version_source();

COMMENT ON TABLE document_parsed_artifacts IS
    'Authoritative immutable parser output for one exact source version; all downstream derivations name the artifact they consumed.';
COMMENT ON COLUMN document_derivations.input_parsed_artifact_id IS
    'Exact immutable parsed artifact consumed by this derivation; mandatory after the migration 032 authority cutover.';
COMMENT ON COLUMN documents.content_hash IS
    'Current source-version SHA-256 projection for listing and duplicate detection; exact parsed bytes live in document_parsed_artifacts.';
COMMENT ON COLUMN document_versions.content_hash IS
    'Immutable SHA-256 identity of the accepted original source bytes; parsed bytes and their independent digest live in document_parsed_artifacts.';
