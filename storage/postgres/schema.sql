-- storage/postgres/schema.sql
-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Enable pgvector
CREATE EXTENSION IF NOT EXISTS vector;

-- Context Registry
CREATE TABLE IF NOT EXISTS context_sources (
    source_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type VARCHAR(100) NOT NULL,
    source_name VARCHAR(255) NOT NULL,
    connection_identifier VARCHAR(500),
    metadata JSONB DEFAULT '{}'::jsonb,
    status VARCHAR(50) DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(source_type, source_name)
);


CREATE TABLE IF NOT EXISTS context_syncs (
    sync_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL
        REFERENCES context_sources(source_id)
        ON DELETE CASCADE,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    status VARCHAR(50) NOT NULL,
    items_fetched INTEGER DEFAULT 0,
    items_created INTEGER DEFAULT 0,
    items_updated INTEGER DEFAULT 0,
    error_message TEXT,
    metadata JSONB DEFAULT '{}'::jsonb
);


-- Artifact Registry
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_type VARCHAR(100) NOT NULL,
    title TEXT,
    content TEXT,
    source_id UUID
        REFERENCES context_sources(source_id)
        ON DELETE SET NULL,
    source_ref VARCHAR(500),
    metadata JSONB DEFAULT '{}'::jsonb,
    version INTEGER DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(source_id, source_ref)
);

-- Provenance
CREATE TABLE IF NOT EXISTS artifact_provenance (
    provenance_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_id UUID NOT NULL
        REFERENCES artifacts(artifact_id)
        ON DELETE CASCADE,
    source_id UUID
        REFERENCES context_sources(source_id)
        ON DELETE SET NULL,
    source_ref VARCHAR(500),
    extraction_method VARCHAR(100),
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Vector / Knowledge Store
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_id UUID
        REFERENCES artifacts(artifact_id)
        ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding VECTOR(384),
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_context_sources_type ON context_sources(source_type);
CREATE INDEX IF NOT EXISTS idx_context_syncs_source ON context_syncs(source_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_type ON artifacts(artifact_type);
CREATE INDEX IF NOT EXISTS idx_artifacts_source ON artifacts(source_id);
CREATE INDEX IF NOT EXISTS idx_provenance_artifact ON artifact_provenance(artifact_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_artifact ON knowledge_chunks(artifact_id);

-- Vector similarity index
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_embedding 
        ON knowledge_chunks USING ivfflat (embedding vector_cosine_ops) 
        WITH (lists = 100);