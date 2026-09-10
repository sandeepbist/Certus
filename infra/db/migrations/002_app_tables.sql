-- ============================================================
-- Nexus AI-OS — Core Application Schema
-- 002_app_tables.sql
-- ============================================================

-- ============================================================
-- 1. TENANT CONFIG & USER PREFERENCES
-- Extends Better Auth's organization and user entities
-- ============================================================

CREATE TABLE IF NOT EXISTS tenant_config (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    organization_id TEXT NOT NULL UNIQUE,  -- Maps to Better Auth organization.id
    plan VARCHAR(50) DEFAULT 'free',       -- free, pro, enterprise
    max_documents INT DEFAULT 100,
    max_storage_bytes BIGINT DEFAULT 536870912, -- 512MB
    max_token_budget_daily INT DEFAULT 100000,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_preferences (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id TEXT NOT NULL UNIQUE,          -- Maps to Better Auth user.id
    organization_id TEXT,                  -- Default active tenant / organization.id
    theme VARCHAR(20) DEFAULT 'system',    -- dark, light, system
    default_model VARCHAR(50) DEFAULT 'gpt-4o-mini',
    default_chunk_strategy VARCHAR(50) DEFAULT 'semantic',
    notification_email BOOLEAN DEFAULT true,
    notification_in_app BOOLEAN DEFAULT true,
    notification_digest BOOLEAN DEFAULT true,
    token_used_today INT DEFAULT 0,
    budget_reset_at TIMESTAMPTZ,
    onboarding_completed BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id TEXT NOT NULL,                 -- Maps to Better Auth user.id
    organization_id TEXT NOT NULL,         -- Maps to Better Auth organization.id
    title VARCHAR(255),
    is_pinned BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_active_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_user ON chat_sessions(user_id, organization_id, last_active_at DESC);

-- ============================================================
-- 2. DOCUMENTS & CHUNKS (RAG PIPELINE)
-- ============================================================

CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id TEXT NOT NULL,                 -- Maps to Better Auth user.id
    tenant_id TEXT NOT NULL,               -- Maps to Better Auth organization.id
    title VARCHAR(500),
    source_type VARCHAR(50) NOT NULL,      -- pdf, markdown, text, docx, html
    mime_type VARCHAR(100) NOT NULL,
    file_size_bytes BIGINT,
    content_hash VARCHAR(64) NOT NULL,     -- SHA-256 for deduplication
    raw_text TEXT,
    chunk_count INT DEFAULT 0,
    entity_count INT DEFAULT 0,
    tags TEXT[] DEFAULT '{}',
    status VARCHAR(50) DEFAULT 'processing', -- processing, ready, error
    error_message TEXT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_documents_tenant_user ON documents(tenant_id, user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);

CREATE TABLE IF NOT EXISTS chunks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,                 -- Maps to Better Auth user.id
    tenant_id TEXT NOT NULL,               -- Maps to Better Auth organization.id
    content TEXT NOT NULL,
    contextualized_content TEXT,           -- With document context prepended
    embedding vector(1536),                -- OpenAI text-embedding-3-small
    chunk_index INT NOT NULL,
    token_count INT,
    start_char INT,
    end_char INT,
    page_number INT,
    section_title VARCHAR(500),
    language VARCHAR(10) DEFAULT 'en',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    search_vector tsvector GENERATED ALWAYS AS (
        to_tsvector('english', content)
    ) STORED
);

-- HNSW Vector Index for Cosine Distance Retrieval
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON chunks
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 200);

-- Full-text Search GIN Index
CREATE INDEX IF NOT EXISTS idx_chunks_search ON chunks USING gin(search_vector);

-- Tenant Isolation Indexes
CREATE INDEX IF NOT EXISTS idx_chunks_user_tenant ON chunks(tenant_id, user_id);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);

-- ============================================================
-- 3. TASKS
-- ============================================================

CREATE TABLE IF NOT EXISTS tasks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    title VARCHAR(500) NOT NULL,
    description TEXT,
    status VARCHAR(50) DEFAULT 'open',     -- open, in_progress, done, cancelled
    priority VARCHAR(20) DEFAULT 'medium', -- low, medium, high, urgent
    due_date TIMESTAMPTZ,
    tags TEXT[] DEFAULT '{}',
    source_document_id UUID REFERENCES documents(id) ON DELETE SET NULL,
    source_agent_run_id UUID,
    trigger_rule JSONB,
    version INT DEFAULT 1,                 -- Optimistic locking
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tasks_tenant_user ON tasks(tenant_id, user_id, status);

-- ============================================================
-- 4. AGENT RUNS (Event-Sourced Trace Logging)
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    session_id UUID REFERENCES chat_sessions(id) ON DELETE SET NULL,
    input_query TEXT NOT NULL,
    plan JSONB,
    model_used VARCHAR(50),
    retrieved_chunk_ids UUID[],
    output_response TEXT,
    citations JSONB,
    -- Cost tracking
    prompt_tokens INT DEFAULT 0,
    completion_tokens INT DEFAULT 0,
    total_tokens INT DEFAULT 0,
    estimated_cost_usd DECIMAL(10, 6),
    -- Latencies
    latency_ms INT,
    retrieval_latency_ms INT,
    llm_latency_ms INT,
    -- Evaluation
    eval_score DECIMAL(3, 2),              -- 0.00 - 1.00
    eval_details JSONB,
    critic_iterations INT DEFAULT 0,
    -- Event Sourcing (Append-only execution stream)
    events JSONB NOT NULL DEFAULT '[]',
    status VARCHAR(50) DEFAULT 'running',  -- running, completed, failed, timeout
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_tenant_user ON agent_runs(tenant_id, user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_session ON agent_runs(session_id);

-- ============================================================
-- 5. LONG-TERM MEMORY (3-Tier Fact Store)
-- ============================================================

CREATE TABLE IF NOT EXISTS memories (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    fact TEXT NOT NULL,
    category VARCHAR(50),                  -- preference, project, person, deadline, concept
    confidence DECIMAL(3, 2) DEFAULT 1.00,
    source_run_id UUID REFERENCES agent_runs(id) ON DELETE SET NULL,
    embedding vector(1536),
    access_count INT DEFAULT 0,
    last_accessed_at TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT true,
    superseded_by UUID REFERENCES memories(id),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memories_user_active ON memories(tenant_id, user_id) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 100);

-- ============================================================
-- 6. AUTOMATION RULES (Temporal Event Routing)
-- ============================================================

CREATE TABLE IF NOT EXISTS automation_rules (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    trigger_event VARCHAR(100) NOT NULL,   -- document_uploaded, memory_extracted, task_created, schedule_cron
    trigger_conditions JSONB NOT NULL DEFAULT '{}',
    actions JSONB NOT NULL DEFAULT '[]',
    is_enabled BOOLEAN DEFAULT true,
    execution_count INT DEFAULT 0,
    last_executed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_automation_rules_tenant ON automation_rules(tenant_id, is_enabled);

-- ============================================================
-- 7. SEMANTIC RESPONSE CACHE
-- ============================================================

CREATE TABLE IF NOT EXISTS semantic_cache (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    query_embedding vector(1536),
    query_text TEXT NOT NULL,
    response_text TEXT NOT NULL,
    response_citations JSONB,
    model_used VARCHAR(50),
    hit_count INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ DEFAULT NOW() + INTERVAL '7 days'
);

CREATE INDEX IF NOT EXISTS idx_semantic_cache_embedding ON semantic_cache
USING hnsw (query_embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 100);

CREATE INDEX IF NOT EXISTS idx_semantic_cache_expiry ON semantic_cache(expires_at);

-- ============================================================
-- 8. NOTIFICATIONS
-- ============================================================

CREATE TABLE IF NOT EXISTS notifications (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    type VARCHAR(50) NOT NULL,             -- document_ready, automation_triggered, usage_warning, task_due
    title VARCHAR(255) NOT NULL,
    body TEXT,
    metadata JSONB DEFAULT '{}',
    action_url VARCHAR(500),
    is_read BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_notifications_user_unread
    ON notifications(user_id, organization_id, is_read, created_at DESC)
    WHERE is_read = false;

-- ============================================================
-- 9. WEBHOOKS & DELIVERIES
-- ============================================================

CREATE TABLE IF NOT EXISTS webhooks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    url VARCHAR(500) NOT NULL,
    events TEXT[] NOT NULL,                 -- {'document_ready', 'automation_triggered', 'task_completed'}
    secret VARCHAR(255) NOT NULL,          -- HMAC-SHA256 signing secret
    is_enabled BOOLEAN DEFAULT true,
    last_triggered_at TIMESTAMPTZ,
    failure_count INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    webhook_id UUID NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
    event_type VARCHAR(50) NOT NULL,
    payload JSONB NOT NULL,
    response_status INT,
    response_body TEXT,
    delivered_at TIMESTAMPTZ DEFAULT NOW(),
    success BOOLEAN DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries ON webhook_deliveries(webhook_id, delivered_at DESC);

-- ============================================================
-- 10. DATA EXPORTS (GDPR Compliance)
-- ============================================================

CREATE TABLE IF NOT EXISTS data_exports (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id TEXT NOT NULL,
    status VARCHAR(50) DEFAULT 'pending',  -- pending, processing, ready, expired
    file_url TEXT,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
