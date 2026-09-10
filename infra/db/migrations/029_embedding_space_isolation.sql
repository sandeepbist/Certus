-- Prevent vectors produced by different models or implementations from being
-- compared in the same coordinate space. The profile is pinned when work is
-- staged and must match the query profile at retrieval time.

ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS embedding_profile VARCHAR(255);

ALTER TABLE document_embedding_jobs
    ADD COLUMN IF NOT EXISTS embedding_profile VARCHAR(255);

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS embedding_profile VARCHAR(255);

UPDATE chunks
SET embedding_profile = CASE
    WHEN metadata->>'embedding_provider' = 'local-lexical-v2'
        THEN 'embedding-space:v1:local:local-lexical-v2:1536'
    WHEN COALESCE(metadata->>'embedding_provider', '') ~ '^[A-Za-z0-9._-]+$'
        THEN 'embedding-space:v1:openai:'
             || (metadata->>'embedding_provider') || ':1536'
    ELSE 'embedding-space:v1:legacy:unversioned:1536'
END
WHERE embedding_profile IS NULL;

WITH generation_profiles AS (
    SELECT document_id,
           processing_generation,
           CASE
               WHEN COUNT(DISTINCT embedding_profile) = 1 THEN MIN(embedding_profile)
               ELSE 'embedding-space:v1:legacy:unversioned:1536'
           END AS embedding_profile
    FROM chunks
    GROUP BY document_id, processing_generation
)
UPDATE document_embedding_jobs AS job
SET embedding_profile = generation.embedding_profile
FROM generation_profiles AS generation
WHERE job.document_id = generation.document_id
  AND job.processing_generation = generation.processing_generation
  AND job.embedding_profile IS NULL;

UPDATE document_embedding_jobs
SET embedding_profile = 'embedding-space:v1:legacy:unversioned:1536'
WHERE embedding_profile IS NULL;

UPDATE memories
SET embedding_profile = CASE
    WHEN embedding_provider = 'local-lexical-v2'
        THEN 'embedding-space:v1:local:local-lexical-v2:1536'
    WHEN COALESCE(embedding_provider, '') ~ '^[A-Za-z0-9._-]+$'
        THEN 'embedding-space:v1:openai:' || embedding_provider || ':1536'
    ELSE 'embedding-space:v1:legacy:unversioned:1536'
END
WHERE embedding_profile IS NULL;

ALTER TABLE chunks
    ALTER COLUMN embedding_profile SET NOT NULL,
    DROP CONSTRAINT IF EXISTS chunks_embedding_profile_check,
    ADD CONSTRAINT chunks_embedding_profile_check CHECK (
        embedding_profile ~ '^embedding-space:v1:(local|openai|legacy):[A-Za-z0-9._-]+:1536$'
    );

ALTER TABLE document_embedding_jobs
    ALTER COLUMN embedding_profile SET NOT NULL,
    DROP CONSTRAINT IF EXISTS document_embedding_jobs_embedding_profile_check,
    ADD CONSTRAINT document_embedding_jobs_embedding_profile_check CHECK (
        embedding_profile ~ '^embedding-space:v1:(local|openai|legacy):[A-Za-z0-9._-]+:1536$'
    );

ALTER TABLE memories
    ALTER COLUMN embedding_profile SET NOT NULL,
    DROP CONSTRAINT IF EXISTS memories_embedding_profile_check,
    ADD CONSTRAINT memories_embedding_profile_check CHECK (
        embedding_profile ~ '^embedding-space:v1:(local|openai|legacy):[A-Za-z0-9._-]+:1536$'
    );

COMMENT ON COLUMN chunks.embedding_profile IS
    'Immutable coordinate-space identifier required for compatible vector retrieval.';
COMMENT ON COLUMN document_embedding_jobs.embedding_profile IS
    'Coordinate-space identifier pinned when the durable embedding job is staged.';
COMMENT ON COLUMN memories.embedding_profile IS
    'Immutable coordinate-space identifier required for compatible memory retrieval.';

CREATE INDEX IF NOT EXISTS idx_chunks_embedding_profile_scope
    ON chunks(tenant_id, user_id, embedding_profile)
    WHERE embedding IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_memories_embedding_profile_scope
    ON memories(tenant_id, user_id, embedding_profile)
    WHERE is_active = true AND embedding IS NOT NULL;
