-- Keep incompatible coordinate spaces out of the same approximate-neighbor
-- graph. PostgreSQL must see the fixed profile predicate at planning time for
-- these partial indexes to be eligible; serving uses the matching closed
-- literal mapping in services/shared/embeddings.py.

DROP INDEX IF EXISTS idx_chunks_embedding;
DROP INDEX IF EXISTS idx_memories_embedding;
DROP INDEX IF EXISTS idx_semantic_cache_embedding;

CREATE INDEX idx_chunks_embedding_local_lexical_v2
    ON chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200)
    WHERE embedding IS NOT NULL
      AND embedding_profile = 'embedding-space:v1:local:local-lexical-v2:1536';

CREATE INDEX idx_chunks_embedding_openai_small
    ON chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200)
    WHERE embedding IS NOT NULL
      AND embedding_profile = 'embedding-space:v1:openai:text-embedding-3-small:1536';

CREATE INDEX idx_chunks_embedding_openai_large
    ON chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200)
    WHERE embedding IS NOT NULL
      AND embedding_profile = 'embedding-space:v1:openai:text-embedding-3-large:1536';

CREATE INDEX idx_memories_embedding_local_lexical_v2
    ON memories USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 100)
    WHERE is_active = true
      AND embedding IS NOT NULL
      AND embedding_profile = 'embedding-space:v1:local:local-lexical-v2:1536';

CREATE INDEX idx_memories_embedding_openai_small
    ON memories USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 100)
    WHERE is_active = true
      AND embedding IS NOT NULL
      AND embedding_profile = 'embedding-space:v1:openai:text-embedding-3-small:1536';

CREATE INDEX idx_memories_embedding_openai_large
    ON memories USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 100)
    WHERE is_active = true
      AND embedding IS NOT NULL
      AND embedding_profile = 'embedding-space:v1:openai:text-embedding-3-large:1536';

COMMENT ON COLUMN chunks.embedding_profile IS
    'Immutable coordinate-space identifier. Each supported serving profile has an isolated HNSW graph.';
COMMENT ON COLUMN memories.embedding_profile IS
    'Immutable coordinate-space identifier. Each supported serving profile has an isolated HNSW graph.';
COMMENT ON TABLE semantic_cache IS
    'Legacy inactive cache schema. Vector lookup is intentionally unindexed until entries carry an immutable embedding profile.';
