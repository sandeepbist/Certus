-- Better Auth owns key generation, hashing, expiry, and rate limits. This table
-- adds immutable Certus workspace/user attribution and scopes so API requests do
-- not have to trust caller-controlled key metadata.

CREATE TABLE IF NOT EXISTS certus_api_keys (
    key_id TEXT PRIMARY KEY REFERENCES apikey(id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    created_by TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    scopes TEXT[] NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT certus_api_keys_scopes_check
        CHECK (
            cardinality(scopes) > 0
            AND scopes <@ ARRAY['read', 'write', 'admin']::TEXT[]
        )
);

CREATE INDEX IF NOT EXISTS idx_certus_api_keys_tenant_created
    ON certus_api_keys(tenant_id, created_at DESC);
