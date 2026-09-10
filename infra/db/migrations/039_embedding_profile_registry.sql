-- Replace repeated embedding-profile strings without an authority row with an
-- immutable registry. This migration does not change the active profile or
-- vector storage; it makes every current producer definition resolvable before
-- the later shadow-index/backfill/cutover work begins.

CREATE TABLE embedding_profiles (
    identifier VARCHAR(255) PRIMARY KEY,
    schema_version VARCHAR(10) NOT NULL,
    provider VARCHAR(20) NOT NULL,
    model VARCHAR(120) NOT NULL,
    dimensions INT NOT NULL,
    distance_metric VARCHAR(20) NOT NULL,
    normalization_profile VARCHAR(80) NOT NULL,
    input_profile VARCHAR(80) NOT NULL,
    storage_profile VARCHAR(80) NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT embedding_profiles_schema_check CHECK (schema_version = 'v1'),
    CONSTRAINT embedding_profiles_provider_check CHECK (
        provider IN ('local', 'openai', 'legacy')
    ),
    CONSTRAINT embedding_profiles_model_check CHECK (
        model ~ '^[A-Za-z0-9._-]+$'
    ),
    CONSTRAINT embedding_profiles_dimensions_check CHECK (dimensions = 1536),
    CONSTRAINT embedding_profiles_distance_check CHECK (distance_metric = 'cosine'),
    CONSTRAINT embedding_profiles_normalization_check CHECK (
        normalization_profile IN (
            'l2_normalized:v1',
            'producer_contract:v1',
            'unknown:v0'
        )
    ),
    CONSTRAINT embedding_profiles_input_check CHECK (
        input_profile = 'plain_text_unprefixed:v1'
    ),
    CONSTRAINT embedding_profiles_storage_check CHECK (
        storage_profile = 'pgvector_float32_cosine:v1'
    ),
    CONSTRAINT embedding_profiles_identifier_check CHECK (
        identifier = 'embedding-space:' || schema_version || ':' || provider
            || ':' || model || ':' || dimensions::TEXT
    )
);

WITH existing_profiles(identifier) AS (
    SELECT embedding_profile FROM chunks
    UNION
    SELECT embedding_profile FROM document_embedding_jobs
    UNION
    SELECT embedding_profile FROM document_derivations
    UNION
    SELECT embedding_profile FROM memories
    UNION ALL
    VALUES
        ('embedding-space:v1:local:local-lexical-v2:1536'),
        ('embedding-space:v1:openai:text-embedding-3-small:1536'),
        ('embedding-space:v1:openai:text-embedding-3-large:1536'),
        ('embedding-space:v1:legacy:unversioned:1536')
), parsed_profiles AS (
    SELECT DISTINCT identifier,
           split_part(identifier, ':', 2) AS schema_version,
           split_part(identifier, ':', 3) AS provider,
           split_part(identifier, ':', 4) AS model,
           split_part(identifier, ':', 5)::INT AS dimensions
    FROM existing_profiles
)
INSERT INTO embedding_profiles (
    identifier, schema_version, provider, model, dimensions,
    distance_metric, normalization_profile, input_profile, storage_profile
)
SELECT identifier,
       schema_version,
       provider,
       model,
       dimensions,
       'cosine',
       CASE provider
           WHEN 'local' THEN 'l2_normalized:v1'
           WHEN 'openai' THEN 'producer_contract:v1'
           ELSE 'unknown:v0'
       END,
       'plain_text_unprefixed:v1',
       'pgvector_float32_cosine:v1'
FROM parsed_profiles;

ALTER TABLE chunks
    ADD CONSTRAINT chunks_embedding_profile_registry_fk
    FOREIGN KEY (embedding_profile) REFERENCES embedding_profiles(identifier)
    ON UPDATE RESTRICT ON DELETE RESTRICT;

ALTER TABLE document_embedding_jobs
    ADD CONSTRAINT document_embedding_jobs_profile_registry_fk
    FOREIGN KEY (embedding_profile) REFERENCES embedding_profiles(identifier)
    ON UPDATE RESTRICT ON DELETE RESTRICT;

ALTER TABLE document_derivations
    ADD CONSTRAINT document_derivations_profile_registry_fk
    FOREIGN KEY (embedding_profile) REFERENCES embedding_profiles(identifier)
    ON UPDATE RESTRICT ON DELETE RESTRICT;

ALTER TABLE memories
    ADD CONSTRAINT memories_embedding_profile_registry_fk
    FOREIGN KEY (embedding_profile) REFERENCES embedding_profiles(identifier)
    ON UPDATE RESTRICT ON DELETE RESTRICT;

CREATE FUNCTION reject_embedding_profile_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'embedding profile definitions are immutable';
END;
$$;

CREATE TRIGGER trg_reject_embedding_profile_mutation
    BEFORE UPDATE OR DELETE ON embedding_profiles
    FOR EACH ROW EXECUTE FUNCTION reject_embedding_profile_mutation();

COMMENT ON TABLE embedding_profiles IS
    'Immutable producer/input/vector-space definitions referenced by every persisted embedding profile.';
COMMENT ON COLUMN embedding_profiles.normalization_profile IS
    'Versioned normalization contract; producer_contract records provider-defined behavior without inventing local transforms.';
