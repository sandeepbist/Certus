-- Fence embedding workers with an explicit lease owner. A worker whose provider
-- call outlives the processing lease must not overwrite work claimed by a newer
-- delivery, and a failed worker must be able to release only its own lease.

ALTER TABLE document_embedding_jobs
    ADD COLUMN IF NOT EXISTS processing_owner TEXT;
