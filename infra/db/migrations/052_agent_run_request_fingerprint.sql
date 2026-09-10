-- Bind idempotent agent-run retries to the complete retrieval-affecting request.
-- Existing rows remain compatible; all new application writes include the hash.

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS request_fingerprint CHAR(64),
    DROP CONSTRAINT IF EXISTS agent_runs_request_fingerprint_sha256_check,
    ADD CONSTRAINT agent_runs_request_fingerprint_sha256_check CHECK (
        request_fingerprint IS NULL
        OR request_fingerprint ~ '^[0-9a-f]{64}$'
    );

COMMENT ON COLUMN agent_runs.request_fingerprint IS
    'SHA-256 of canonical query, session, model, replay inputs, and product-selected document scope for exact idempotent retries.';
