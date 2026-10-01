-- findings.bulk_imported: set on Findings a file upload or S3 import stored.
-- Those are never triaged at ingest; the enrichment sweep rates them after
-- live Findings and never responds to them.
--
-- create_all builds findings and never alters it once it exists, and every read
-- of findings fails with UndefinedColumn until the column is there. So db-init
-- adds it, as the table's owner, rather than waiting on scripts/migrate_schema.py
-- (which has the same steps for installs that run it by hand).
--
-- Rows already stored read NULL rather than taking the default, which would
-- call them all live. The second block marks the unrated ones by what used to
-- tell an upload apart: no event time, or stored over 24h after it. Two blocks,
-- two transactions, so the ALTER's lock is released before the UPDATE scans.
--
-- Idempotent: compose's db-seed re-runs this on every `up`. Safe when findings
-- is absent (fresh install: create_all builds the column).

DO $$
BEGIN
    IF to_regclass('findings') IS NULL THEN
        RAISE NOTICE '37_findings_bulk_imported: findings table absent (fresh DB), nothing to alter';
        RETURN;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'findings' AND column_name = 'bulk_imported'
    ) THEN
        ALTER TABLE findings ADD COLUMN bulk_imported BOOLEAN;
        ALTER TABLE findings ALTER COLUMN bulk_imported SET DEFAULT false;
    END IF;
END $$;

-- The predicate is UNRATED_WHERE in core/storage/models/finding.py.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'findings' AND column_name = 'bulk_imported'
    ) THEN
        RETURN;
    END IF;

    UPDATE findings SET bulk_imported =
        (timestamp IS NULL OR created_at - timestamp > interval '24 hours')
    WHERE bulk_imported IS NULL
      AND (ai_enrichment IS NULL
           OR (ai_enrichment ? 'ai_triage_error' AND NOT (ai_enrichment ? 'ai_triage')));
END $$;

-- Outside a DO block: CONCURRENTLY can't run in a transaction. Errors harmlessly on a fresh DB.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_finding_unrated_sweep
    ON findings (bulk_imported, created_at)
    WHERE (ai_enrichment IS NULL
           OR (ai_enrichment ? 'ai_triage_error' AND NOT (ai_enrichment ? 'ai_triage')));
