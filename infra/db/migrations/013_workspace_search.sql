-- Add indexed, stored search projections for workspace records that previously
-- depended on unbounded pattern scans. Chunk content already has its own GIN
-- index from the base schema.
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS title_search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english'::regconfig, COALESCE(title, ''))
    ) STORED;

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector(
            'english'::regconfig,
            COALESCE(title, '') || ' ' || COALESCE(description, '')
        )
    ) STORED;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english'::regconfig, COALESCE(fact, ''))
    ) STORED;

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector(
            'english'::regconfig,
            COALESCE(input_query, '') || ' ' || COALESCE(output_response, '')
        )
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_documents_title_search
    ON documents USING gin(title_search_vector)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_tasks_search
    ON tasks USING gin(search_vector);

CREATE INDEX IF NOT EXISTS idx_memories_search
    ON memories USING gin(search_vector)
    WHERE is_active = true;

CREATE INDEX IF NOT EXISTS idx_agent_runs_search
    ON agent_runs USING gin(search_vector);

