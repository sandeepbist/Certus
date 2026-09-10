-- Let embedding workers fairly discover durable generation work without
-- duplicating candidate authority into a second queue.

CREATE INDEX idx_workspace_embedding_generation_worker_dispatch
    ON workspace_embedding_generations(embedding_profile, updated_at, id)
    WHERE status = 'building';

COMMENT ON INDEX idx_workspace_embedding_generation_worker_dispatch IS
    'Profile-compatible fair scheduling of open workspace embedding generations by the existing embedding workers.';

COMMENT ON TABLE workspace_embedding_generations IS
    'Exact workspace embedding snapshots; building candidates are a durable PostgreSQL worker queue and approved active generations are runtime serving authorities.';

COMMENT ON TABLE chunk_embedding_vectors IS
    'Versioned workspace vectors with owner-fenced worker leases; completed payloads are immutable and only generation-derived serving membership may change.';
