-- Allow findings.anomaly_score and findings.timestamp to be NULL.
--
-- LogLM rows can carry a classification and embedding without a sequence-level
-- score or event time. Ingest used to invent 0.0 / utcnow() because these
-- columns were NOT NULL. Severity is already nullable.
--
-- A NEW migration file (not edits to 01_init_schema.sql) is what reaches
-- existing deployments: dbInit only runs a filename it hasn't applied before.
-- Idempotent and self-guarding: safe to re-run, safe when the findings table
-- is absent (fresh install).

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables WHERE table_name = 'findings'
    ) THEN
        RAISE NOTICE '25_nullable_finding_score_timestamp: findings table absent (fresh DB), nothing to alter';
        RETURN;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'findings' AND column_name = 'anomaly_score'
    ) THEN
        ALTER TABLE findings ALTER COLUMN anomaly_score DROP NOT NULL;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'findings' AND column_name = 'timestamp'
    ) THEN
        ALTER TABLE findings ALTER COLUMN timestamp DROP NOT NULL;
    END IF;
    RAISE NOTICE '25_nullable_finding_score_timestamp: anomaly_score and timestamp are nullable';
END $$;
