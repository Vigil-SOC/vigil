-- Re-key ai_decision_logs.agent_id onto the action vocabulary (GH #476)
-- Decision rows are grouped by the action performed, not the role that
-- performed it, and the registry in core/agents/builtins.py is now the sole
-- definition of that mapping. Rows written before the registry landed carry
-- role ids (or the daemon's ad-hoc "orchestrator"); rewrite them so the
-- AI-Decisions filter sees one vocabulary.
--
-- ai_decision_logs is created by SQLAlchemy create_all() on backend startup,
-- not by this init/ directory, so on a fresh database it does not yet exist
-- when this file runs under /docker-entrypoint-initdb.d (ON_ERROR_STOP=1).
-- Guard on its existence and skip cleanly when absent -- there are no rows to
-- re-key on a fresh install anyway. Mirrors 17_loglm_setup.sql. Idempotent:
-- the target ids are not in the WHERE set, so re-running is a no-op.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'ai_decision_logs'
    ) THEN
        RAISE NOTICE '18_agent_decision_ids: ai_decision_logs absent (fresh DB), skipping';
        RETURN;
    END IF;

    UPDATE ai_decision_logs SET agent_id = CASE agent_id
        WHEN 'investigator'    THEN 'investigation'
        WHEN 'correlator'      THEN 'correlation'
        WHEN 'reporter'        THEN 'reporting'
        WHEN 'responder'       THEN 'response'
        WHEN 'threat_hunter'   THEN 'threat_hunt'
        WHEN 'mitre_analyst'   THEN 'mitre_mapping'
        WHEN 'malware_analyst' THEN 'malware_analysis'
        WHEN 'network_analyst' THEN 'network_analysis'
        WHEN 'auto_responder'  THEN 'auto_response'
        WHEN 'orchestrator'    THEN 'orchestration'
        ELSE agent_id
    END
    WHERE agent_id IN (
        'investigator', 'correlator', 'reporter', 'responder', 'threat_hunter',
        'mitre_analyst', 'malware_analyst', 'network_analyst', 'auto_responder',
        'orchestrator'
    );

    COMMENT ON COLUMN ai_decision_logs.agent_id IS
        'Action id from core/agents/builtins.py (e.g. investigation, '
        'correlation), or orchestration for the autonomous loop''s own decisions';
END $$;
