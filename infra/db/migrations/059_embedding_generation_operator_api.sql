-- Bound operator-facing generation history and evaluation metadata.

ALTER TABLE workspace_embedding_generations
    ADD CONSTRAINT workspace_embedding_generation_evaluation_size_check
    CHECK (octet_length(evaluation_report::TEXT) <= 65536) NOT VALID;

ALTER TABLE workspace_embedding_generations
    VALIDATE CONSTRAINT workspace_embedding_generation_evaluation_size_check;

CREATE INDEX idx_workspace_embedding_generation_status_history
    ON workspace_embedding_generations(
        tenant_id, user_id, status, created_at DESC, id DESC
    );

COMMENT ON CONSTRAINT workspace_embedding_generation_evaluation_size_check
    ON workspace_embedding_generations IS
    'Prevents unbounded evaluator metadata from entering operator responses, exports, and retained control-plane history.';
