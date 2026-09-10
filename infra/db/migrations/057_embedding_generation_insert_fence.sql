-- An active generation is immutable serving state. Even a privileged direct
-- insert must not be able to append a new candidate after coverage evaluation
-- and activation.

CREATE OR REPLACE FUNCTION enforce_chunk_embedding_vector_serving_contract()
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
    IF TG_OP = 'INSERT' AND generation_status <> 'building' THEN
        RAISE EXCEPTION 'embedding candidates can be added only while a generation is building';
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

COMMENT ON FUNCTION enforce_chunk_embedding_vector_serving_contract() IS
    'Inherits profile identity, derives serving membership, and rejects candidate insertion after generation evaluation.';
