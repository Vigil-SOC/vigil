-- Promote findings.mitre_predictions JSONB to finding_mitre_predictions.
--
-- The map is one-to-many; ATT&CK readers need to filter / order / group by
-- technique_id without loading every finding. create_all builds the child
-- table on a fresh database; this file is what reaches existing deployments
-- (backfill + DROP COLUMN). Idempotent and self-guarding: safe to re-run,
-- safe when the findings table is absent (fresh install).
--
-- docker-compose mounts database/init/ at /docker-entrypoint-initdb.d, which
-- Postgres runs only when initialising an empty data directory. An existing
-- local volume will not pick this file up — recreate the volume or apply the
-- SQL by hand.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables WHERE table_name = 'findings'
    ) THEN
        RAISE NOTICE '24_finding_mitre_predictions: findings table absent (fresh DB), nothing to migrate';
        RETURN;
    END IF;

    CREATE TABLE IF NOT EXISTS finding_mitre_predictions (
        finding_id VARCHAR(50) NOT NULL REFERENCES findings(finding_id) ON DELETE CASCADE,
        technique_id TEXT NOT NULL,
        confidence DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (finding_id, technique_id)
    );

    CREATE INDEX IF NOT EXISTS idx_finding_mitre_predictions_technique_confidence
        ON finding_mitre_predictions (technique_id, confidence DESC);

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'findings' AND column_name = 'mitre_predictions'
    ) THEN
        INSERT INTO finding_mitre_predictions (finding_id, technique_id, confidence)
        SELECT f.finding_id, kv.key, (kv.value)::text::double precision
        FROM findings f
        CROSS JOIN LATERAL jsonb_each(f.mitre_predictions) AS kv
        WHERE jsonb_typeof(f.mitre_predictions) = 'object'
          AND jsonb_typeof(kv.value) = 'number'
        ON CONFLICT DO NOTHING;

        ALTER TABLE findings DROP COLUMN IF EXISTS mitre_predictions;
        RAISE NOTICE '24_finding_mitre_predictions: backfilled and dropped findings.mitre_predictions';
    ELSE
        RAISE NOTICE '24_finding_mitre_predictions: mitre_predictions column already absent';
    END IF;
END $$;
