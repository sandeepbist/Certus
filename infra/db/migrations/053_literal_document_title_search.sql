-- Preserve literal filename/title lookup when web-search syntax contains
-- punctuation (for example, hyphens interpreted as NOT operators).

CREATE INDEX IF NOT EXISTS idx_documents_active_title_trigram
    ON documents USING gin (lower(title) gin_trgm_ops)
    WHERE deleted_at IS NULL;

COMMENT ON INDEX idx_documents_active_title_trigram IS
    'Tenant-filtered document picker fallback for indexed literal title substrings alongside full-text search.';
