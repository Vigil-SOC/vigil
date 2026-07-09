-- Vigil SOC Database Initialization
-- This script is automatically run when the PostgreSQL container first starts

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
-- pg_trgm powers GIN trigram indexes used for full-text search on findings
-- (idx_finding_description and similar). Without it the ORM's create_all
-- fails with: operator class "gin_trgm_ops" does not exist for access
-- method "gin".
CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- pgvector powers the findings.embedding vector(768) column + HNSW ANN index
-- (idx_finding_embedding_hnsw). Without it the ORM's create_all fails with:
-- type "vector" does not exist. Requires a pgvector-capable image
-- (pgvector/pgvector:pg16) — the stock postgres:16 image does NOT ship it.
CREATE EXTENSION IF NOT EXISTS vector;

-- Create indexes for better performance (SQLAlchemy will create tables)
-- These will be created if they don't already exist

-- Set timezone to UTC
SET timezone = 'UTC';

-- Log initialization
DO $$
BEGIN
    RAISE NOTICE 'Vigil SOC database initialized successfully';
END $$;

