-- One current projection target per document; lifecycle acceptance and repair
-- scheduling commit together. Workers release SQL locks before Neo4j I/O.
CREATE TABLE document_graph_state (
    document_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    acknowledged_revision BIGINT NOT NULL DEFAULT 0,
    lease_owner UUID,
    lease_until TIMESTAMPTZ,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    queued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempts INT NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 1000),
    blocked_reason TEXT CHECK (length(blocked_reason) <= 1000),
    last_error TEXT CHECK (length(last_error) <= 1000),
    FOREIGN KEY (document_id, tenant_id, user_id)
        REFERENCES documents(id, tenant_id, user_id) ON DELETE CASCADE,
    CHECK (acknowledged_revision BETWEEN 0 AND revision),
    CHECK ((lease_owner IS NULL) = (lease_until IS NULL))
);

CREATE INDEX idx_document_graph_state_due
    ON document_graph_state(available_at, document_id)
    WHERE acknowledged_revision < revision AND blocked_reason IS NULL;

CREATE INDEX idx_document_graph_state_pending
    ON document_graph_state(queued_at)
    WHERE acknowledged_revision < revision;

CREATE OR REPLACE FUNCTION queue_document_graph_projection()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.current_version_id IS NULL THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND ROW(
        OLD.current_version_id, OLD.title, OLD.deleted_at, OLD.entity_count
    ) IS NOT DISTINCT FROM ROW(
        NEW.current_version_id, NEW.title, NEW.deleted_at, NEW.entity_count
    ) THEN
        RETURN NEW;
    END IF;
    INSERT INTO document_graph_state(document_id, tenant_id, user_id)
    VALUES (NEW.id, NEW.tenant_id, NEW.user_id)
    ON CONFLICT (document_id) DO UPDATE
    SET revision = document_graph_state.revision + 1,
        lease_owner = NULL, lease_until = NULL,
        available_at = NOW(), queued_at = NOW(), attempts = 0,
        blocked_reason = NULL, last_error = NULL;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_queue_document_graph_projection
AFTER INSERT OR UPDATE OF current_version_id, title, deleted_at, entity_count
ON documents FOR EACH ROW EXECUTE FUNCTION queue_document_graph_projection();

-- Match normal document-then-projection lock order during initial backfill.
WITH source AS MATERIALIZED (
    SELECT id, tenant_id, user_id FROM documents
    WHERE current_version_id IS NOT NULL ORDER BY id FOR UPDATE
)
INSERT INTO document_graph_state(document_id, tenant_id, user_id)
SELECT id, tenant_id, user_id FROM source;

CREATE OR REPLACE FUNCTION rearm_completed_document_graph_snapshot()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'completed' AND OLD.status IS DISTINCT FROM NEW.status THEN
        UPDATE document_graph_state AS state
        SET revision = state.revision + 1, lease_owner = NULL, lease_until = NULL,
            available_at = NOW(), queued_at = NOW(), attempts = 0,
            blocked_reason = NULL, last_error = NULL
        FROM documents AS document
        WHERE document.id = state.document_id
          AND document.current_version_id = NEW.document_version_id
          AND document.id = NEW.document_id
          AND document.tenant_id = NEW.tenant_id AND document.user_id = NEW.user_id
          AND state.tenant_id = document.tenant_id AND state.user_id = document.user_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_rearm_completed_document_graph_snapshot
AFTER UPDATE OF status ON document_upload_intents
FOR EACH ROW EXECUTE FUNCTION rearm_completed_document_graph_snapshot();

-- Completed upload responses are already immutable (migration 044). Retain
-- their exact entity snapshots for as long as their source versions exist.
CREATE OR REPLACE FUNCTION retain_document_graph_snapshot()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'completed' AND EXISTS (
        SELECT 1 FROM document_versions
        WHERE id = OLD.document_version_id AND document_id = OLD.document_id
          AND tenant_id = OLD.tenant_id AND user_id = OLD.user_id
    ) THEN
        RAISE EXCEPTION 'retained document versions require their completed upload snapshot';
    END IF;
    RETURN OLD;
END;
$$;

CREATE TRIGGER trg_retain_document_graph_snapshot
BEFORE DELETE ON document_upload_intents
FOR EACH ROW EXECUTE FUNCTION retain_document_graph_snapshot();
