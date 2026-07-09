-- Migrate findings.embedding to a fixed-width pgvector column + HNSW index.
--
-- Fresh installs get the vector(768) column directly from the ORM's
-- create_all (see database/models.py) and the `vector` extension from
-- 01_init_schema.sql, so this file is a no-op there (the findings table
-- doesn't exist yet when init SQL runs). This migration exists for EXISTING
-- deployments whose findings.embedding is still float8[] (ARRAY(Float)):
-- it coerces every stored embedding to exactly 768 dims (pad/truncate),
-- converts the column to vector(768), and builds the HNSW cosine index.
--
-- Idempotent and self-guarding: safe to re-run and safe when the table is
-- absent or already migrated.

CREATE EXTENSION IF NOT EXISTS vector;

DO $$
DECLARE
    col_type text;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables WHERE table_name = 'findings'
    ) THEN
        RAISE NOTICE '17_pgvector: findings table absent (fresh DB), skipping';
        RETURN;
    END IF;

    SELECT data_type INTO col_type
    FROM information_schema.columns
    WHERE table_name = 'findings' AND column_name = 'embedding';

    IF col_type = 'ARRAY' THEN
        RAISE NOTICE '17_pgvector: coercing existing embeddings to 768 dims';
        -- A fixed-width vector(768) cast requires every row to be exactly
        -- 768-dimensional; pad short vectors with zeros, truncate long ones.
        UPDATE findings
        SET embedding = CASE
            WHEN array_length(embedding, 1) IS NULL
                THEN array_fill(0::float8, ARRAY[768])
            WHEN array_length(embedding, 1) < 768
                THEN embedding || array_fill(
                    0::float8, ARRAY[768 - array_length(embedding, 1)]
                )
            WHEN array_length(embedding, 1) > 768
                THEN embedding[1:768]
            ELSE embedding
        END
        WHERE array_length(embedding, 1) IS DISTINCT FROM 768;

        RAISE NOTICE '17_pgvector: converting embedding column to vector(768)';
        EXECUTE 'ALTER TABLE findings '
             || 'ALTER COLUMN embedding TYPE vector(768) '
             || 'USING embedding::vector(768)';
    ELSE
        RAISE NOTICE
            '17_pgvector: embedding column type is %, assuming already migrated',
            col_type;
    END IF;

    -- Matches idx_finding_embedding_hnsw from the ORM (database/models.py).
    EXECUTE 'CREATE INDEX IF NOT EXISTS idx_finding_embedding_hnsw '
         || 'ON findings USING hnsw (embedding vector_cosine_ops)';
END $$;
