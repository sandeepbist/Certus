-- Normalize task lifecycle values and enforce runtime API invariants.

UPDATE tasks SET status = 'pending' WHERE status = 'open';
UPDATE tasks SET status = 'completed' WHERE status = 'done';

ALTER TABLE tasks
    ALTER COLUMN status SET DEFAULT 'pending',
    ALTER COLUMN status SET NOT NULL,
    ALTER COLUMN priority SET NOT NULL,
    ALTER COLUMN version SET NOT NULL;

ALTER TABLE tasks
    DROP CONSTRAINT IF EXISTS tasks_status_check,
    ADD CONSTRAINT tasks_status_check
        CHECK (status IN ('pending', 'in_progress', 'completed', 'cancelled')),
    DROP CONSTRAINT IF EXISTS tasks_priority_check,
    ADD CONSTRAINT tasks_priority_check
        CHECK (priority IN ('low', 'medium', 'high', 'urgent')),
    DROP CONSTRAINT IF EXISTS tasks_version_check,
    ADD CONSTRAINT tasks_version_check CHECK (version > 0);

CREATE INDEX IF NOT EXISTS idx_tasks_tenant_user_updated
    ON tasks(tenant_id, user_id, updated_at DESC);

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS embedding_provider VARCHAR(100);

ALTER TABLE memories
    DROP CONSTRAINT IF EXISTS memories_confidence_check,
    ADD CONSTRAINT memories_confidence_check
        CHECK (confidence >= 0 AND confidence <= 1);

WITH ranked_memories AS (
    SELECT id,
           FIRST_VALUE(id) OVER (
               PARTITION BY tenant_id, user_id, lower(fact)
               ORDER BY created_at DESC, id DESC
           ) AS retained_id,
           ROW_NUMBER() OVER (
               PARTITION BY tenant_id, user_id, lower(fact)
               ORDER BY created_at DESC, id DESC
           ) AS duplicate_rank
    FROM memories
    WHERE is_active = true
)
UPDATE memories AS memory
SET is_active = false,
    superseded_by = ranked_memories.retained_id
FROM ranked_memories
WHERE memory.id = ranked_memories.id
  AND ranked_memories.duplicate_rank > 1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_active_fact_unique
    ON memories(tenant_id, user_id, lower(fact))
    WHERE is_active = true;
