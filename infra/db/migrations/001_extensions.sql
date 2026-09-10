-- ============================================================
-- Nexus AI-OS — Extensions Migration
-- 001_extensions.sql
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";    -- UUID generation
CREATE EXTENSION IF NOT EXISTS vector;         -- pgvector for embeddings
CREATE EXTENSION IF NOT EXISTS pg_trgm;        -- Trigram similarity for fuzzy search
