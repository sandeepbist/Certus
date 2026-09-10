-- Keep queue retries idempotent and scope document deduplication to a workspace.

WITH duplicate_chunks AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY document_id, chunk_index
               ORDER BY created_at DESC, id DESC
           ) AS duplicate_rank
    FROM chunks
)
DELETE FROM chunks
WHERE id IN (
    SELECT id FROM duplicate_chunks WHERE duplicate_rank > 1
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_document_position_unique
    ON chunks(document_id, chunk_index);

ALTER TABLE documents
    DROP CONSTRAINT IF EXISTS documents_user_id_content_hash_key;

CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_tenant_user_hash_unique
    ON documents(tenant_id, user_id, content_hash);
