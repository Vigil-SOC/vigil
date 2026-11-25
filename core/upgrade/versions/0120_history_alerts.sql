-- One row per alert id in a file import (#5091). A connected pull writes none:
-- it hands write_import an empty alert list. An import already stored stays
-- empty; nothing here copies episodic_sightings back into rows.
-- dropped_columns is {column name: how many alert rows carried it} for names
-- whose values were not stored. Existing imports have none.

ALTER TABLE public.history_imports
    ADD COLUMN IF NOT EXISTS dropped_columns JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS public.history_alerts (
    id BIGSERIAL PRIMARY KEY,
    import_id TEXT NOT NULL
        REFERENCES public.history_imports(id) ON DELETE CASCADE,
    alert_id TEXT NOT NULL,
    -- Null when the row named no parseable time. The row is still kept.
    alert_time TIMESTAMP WITH TIME ZONE,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    rule_id TEXT,
    severity TEXT NOT NULL DEFAULT 'unknown'
        CONSTRAINT history_alerts_severity_check
        CHECK (severity IN ('critical', 'high', 'medium', 'low', 'info', 'unknown')),
    entity_keys TEXT[] NOT NULL DEFAULT '{}',
    CONSTRAINT history_alerts_import_alert_key UNIQUE (import_id, alert_id)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.history_alerts TO vigil_app;
GRANT USAGE, SELECT ON SEQUENCE public.history_alerts_id_seq TO vigil_app;
