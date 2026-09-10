-- Make an approved workspace embedding generation an actual serving authority.
-- Candidate vectors inherit an immutable coordinate-space identifier, while a
-- derived serving marker keeps retired, stale, and building generations out of
-- the physical ANN graph.

ALTER TABLE chunk_embedding_vectors
    ADD COLUMN embedding_profile VARCHAR(255),
    ADD COLUMN is_serving BOOLEAN NOT NULL DEFAULT false;

UPDATE chunk_embedding_vectors AS candidate
SET embedding_profile = generation.embedding_profile,
    is_serving = (
        generation.status = 'active'
        AND candidate.status = 'embedded'
    )
FROM workspace_embedding_generations AS generation
WHERE generation.id = candidate.generation_id;

ALTER TABLE chunk_embedding_vectors
    ALTER COLUMN embedding_profile SET NOT NULL,
    ADD CONSTRAINT chunk_embedding_vectors_profile_fk
        FOREIGN KEY (embedding_profile)
        REFERENCES embedding_profiles(identifier)
        ON UPDATE RESTRICT ON DELETE RESTRICT;

CREATE FUNCTION enforce_chunk_embedding_vector_serving_contract()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    generation_profile VARCHAR(255);
    generation_status VARCHAR(20);
    expected_serving BOOLEAN;
BEGIN
    SELECT embedding_profile, status
    INTO generation_profile, generation_status
    FROM workspace_embedding_generations
    WHERE id = NEW.generation_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'embedding candidate generation does not exist';
    END IF;

    IF NEW.embedding_profile IS NULL THEN
        NEW.embedding_profile := generation_profile;
    ELSIF NEW.embedding_profile IS DISTINCT FROM generation_profile THEN
        RAISE EXCEPTION 'embedding candidate profile differs from its generation';
    END IF;

    expected_serving := generation_status = 'active' AND NEW.status = 'embedded';
    IF NEW.is_serving IS DISTINCT FROM expected_serving THEN
        RAISE EXCEPTION 'embedding candidate serving state differs from its generation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_enforce_chunk_embedding_vector_serving_contract
    BEFORE INSERT OR UPDATE OF generation_id, embedding_profile, status, is_serving
    ON chunk_embedding_vectors
    FOR EACH ROW EXECUTE FUNCTION enforce_chunk_embedding_vector_serving_contract();

CREATE OR REPLACE FUNCTION protect_chunk_embedding_vector_identity()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.generation_id IS DISTINCT FROM NEW.generation_id
       OR OLD.chunk_id IS DISTINCT FROM NEW.chunk_id
       OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.user_id IS DISTINCT FROM NEW.user_id
       OR OLD.content_sha256 IS DISTINCT FROM NEW.content_sha256
       OR OLD.embedding_profile IS DISTINCT FROM NEW.embedding_profile
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'chunk embedding snapshot identity is immutable';
    END IF;
    IF OLD.status = 'embedded'
       AND (
            OLD.status IS DISTINCT FROM NEW.status
            OR OLD.embedding IS DISTINCT FROM NEW.embedding
            OR OLD.provider_metadata IS DISTINCT FROM NEW.provider_metadata
            OR OLD.attempt_count IS DISTINCT FROM NEW.attempt_count
            OR OLD.last_error_code IS DISTINCT FROM NEW.last_error_code
            OR OLD.last_error IS DISTINCT FROM NEW.last_error
            OR OLD.last_attempt_at IS DISTINCT FROM NEW.last_attempt_at
            OR OLD.embedded_at IS DISTINCT FROM NEW.embedded_at
            OR OLD.lease_owner IS DISTINCT FROM NEW.lease_owner
            OR OLD.leased_at IS DISTINCT FROM NEW.leased_at
            OR OLD.available_at IS DISTINCT FROM NEW.available_at
       ) THEN
        RAISE EXCEPTION 'completed chunk embeddings are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION sync_chunk_embedding_vector_serving_state()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status IS DISTINCT FROM NEW.status THEN
        UPDATE chunk_embedding_vectors
        SET is_serving = (NEW.status = 'active' AND status = 'embedded'),
            updated_at = NOW()
        WHERE generation_id = NEW.id
          AND is_serving IS DISTINCT FROM (
              NEW.status = 'active' AND status = 'embedded'
          );
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_sync_chunk_embedding_vector_serving_state
    AFTER UPDATE OF status ON workspace_embedding_generations
    FOR EACH ROW EXECUTE FUNCTION sync_chunk_embedding_vector_serving_state();

DROP INDEX IF EXISTS idx_chunk_embedding_vectors_hnsw;

CREATE INDEX idx_chunk_embedding_vectors_local_lexical_v2
    ON chunk_embedding_vectors USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200)
    WHERE status = 'embedded'
      AND is_serving = true
      AND embedding IS NOT NULL
      AND embedding_profile = 'embedding-space:v1:local:local-lexical-v2:1536';

CREATE INDEX idx_chunk_embedding_vectors_openai_small
    ON chunk_embedding_vectors USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200)
    WHERE status = 'embedded'
      AND is_serving = true
      AND embedding IS NOT NULL
      AND embedding_profile = 'embedding-space:v1:openai:text-embedding-3-small:1536';

CREATE INDEX idx_chunk_embedding_vectors_openai_large
    ON chunk_embedding_vectors USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200)
    WHERE status = 'embedded'
      AND is_serving = true
      AND embedding IS NOT NULL
      AND embedding_profile = 'embedding-space:v1:openai:text-embedding-3-large:1536';

COMMENT ON COLUMN chunk_embedding_vectors.embedding_profile IS
    'Immutable coordinate-space identifier inherited from the owning workspace generation.';
COMMENT ON COLUMN chunk_embedding_vectors.is_serving IS
    'Derived from generation status; only complete vectors in the active generation enter ANN serving indexes.';
COMMENT ON TABLE chunk_embedding_vectors IS
    'Versioned workspace vectors. Completed payloads are immutable; only generation-derived serving membership may change.';
