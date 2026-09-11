-- Drop the findings embedding column + its HNSW index.
--
-- Vigil no longer stores a per-finding embedding. A cross-source vector
-- column was low-signal anyway — only LogLM ever supplied a real vector;
-- every other source wrote placeholder zeros.
--
-- A NEW migration file (not edits to 17_loglm_setup.sql) is what reaches
-- existing deployments: dbInit only runs a filename it hasn't applied before.
-- Idempotent and self-guarding: safe to re-run, safe when the findings table
-- or the column is already absent (fresh install).

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables WHERE table_name = 'findings'
    ) THEN
        RAISE NOTICE '23_drop_finding_embedding: findings table absent (fresh DB), nothing to drop';
        RETURN;
    END IF;

    DROP INDEX IF EXISTS idx_finding_embedding_hnsw;
    ALTER TABLE findings DROP COLUMN IF EXISTS embedding;
    RAISE NOTICE '23_drop_finding_embedding: embedding column + HNSW index dropped';
END $$;
