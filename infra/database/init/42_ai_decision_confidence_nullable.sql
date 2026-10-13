-- Allow ai_decision_logs.confidence_score to be NULL.
--
-- The daemon's triage logs every verdict it parses, and a reply can carry a
-- severity and an action without a usable confidence. Logging those at 0 made
-- "not given" look like a real 0. Rows already written keep their value.
--
-- ai_decision_logs is created by SQLAlchemy create_all, which builds the column
-- nullable on a fresh install, so this file skips cleanly when the table is
-- absent. A file of its own: the Helm init job records each file once it has
-- run. Only the table's owner can alter it; scripts/migrate_schema.py covers a
-- database this file never reached, or one where this file's role does not own
-- the table.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'ai_decision_logs'
    ) THEN
        RAISE NOTICE '42_ai_decision_confidence_nullable: ai_decision_logs absent (fresh DB), skipping';
        RETURN;
    END IF;

    ALTER TABLE ai_decision_logs ALTER COLUMN confidence_score DROP NOT NULL;
END $$;
