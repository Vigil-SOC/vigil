-- LogLM feature setup: pgvector embedding column/index + the loglm.view grant.
--
-- This is a NEW migration file rather than edits to 01/06 on purpose: the
-- dbInit path only runs a file whose name it hasn't applied before, so edits to
-- already-shipped files never reach existing deployments — a new filename does.
-- Idempotent and self-guarding: safe to re-run, and safe when the findings
-- table is absent (fresh install) or already migrated.

CREATE EXTENSION IF NOT EXISTS vector;

DO $$
DECLARE
    col_type text;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables WHERE table_name = 'findings'
    ) THEN
        RAISE NOTICE '17_loglm_setup: findings table absent (fresh DB), skipping embedding migration';
        RETURN;
    END IF;

    SELECT data_type INTO col_type
    FROM information_schema.columns
    WHERE table_name = 'findings' AND column_name = 'embedding';

    IF col_type = 'ARRAY' THEN
        RAISE NOTICE '17_loglm_setup: coercing existing embeddings to 768 dims';
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

        RAISE NOTICE '17_loglm_setup: converting embedding column to vector(768)';
        EXECUTE 'ALTER TABLE findings '
             || 'ALTER COLUMN embedding TYPE vector(768) '
             || 'USING embedding::vector(768)';
    ELSE
        RAISE NOTICE
            '17_loglm_setup: embedding column type is %, assuming already migrated',
            col_type;
    END IF;

    -- Matches idx_finding_embedding_hnsw from the ORM (database/models.py).
    EXECUTE 'CREATE INDEX IF NOT EXISTS idx_finding_embedding_hnsw '
         || 'ON findings USING hnsw (embedding vector_cosine_ops)';
END $$;

-- Grant the LogLM page-extension view permission to analyst-and-above roles.
-- Runs on both fresh and existing deployments (see the file header); the WHERE
-- guard makes it idempotent. The extension manifest declares this permission;
-- without it the LogLM tab is hidden outside DEV_MODE. `roles` always exists
-- here — 06_auth_tables.sql runs before this file on every path.
UPDATE roles
SET permissions = permissions || '{"loglm.view": true}'::jsonb,
    updated_at = NOW()
WHERE role_id IN ('role-analyst', 'role-senior-analyst', 'role-manager', 'role-admin')
  AND COALESCE((permissions->>'loglm.view')::boolean, false) = false;
