-- History imports (#4886, epic #4885): the import's own record and its tickets.
-- Memory's verdict and sighting rows stay under investigation_kind 'imported';
-- the imported marker stops being written, and this table replaces it.
CREATE TABLE IF NOT EXISTS public.history_imports (
    -- '<source system>:<hash>' for a file, '<source>:connected' for a pull.
    id TEXT PRIMARY KEY,
    source_system TEXT NOT NULL,
    -- 'running' with nobody holding the import lock reads "didn't complete".
    status TEXT NOT NULL
        CONSTRAINT history_imports_status_check
        CHECK (status IN ('running', 'done', 'failed')),
    started_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    finished_at TIMESTAMP WITH TIME ZONE,
    error TEXT,
    rows_read INTEGER NOT NULL DEFAULT 0,
    tickets_kept INTEGER NOT NULL DEFAULT 0,
    verdicts_written INTEGER NOT NULL DEFAULT 0,
    sightings_written INTEGER NOT NULL DEFAULT 0,
    -- {"tickets": {reason: n}, "alerts": {reason: n}}; bulk-closed tickets are
    -- kept in history_tickets and counted here as left out of memory.
    left_out JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Tickets the bulk-close check could not test: no closer field.
    bulk_not_tested INTEGER NOT NULL DEFAULT 0,
    severity_unknown INTEGER NOT NULL DEFAULT 0,
    -- {raw disposition value: n} for values RAW_LABELS does not list.
    unmapped JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- A connected pull's window: its days and when it was read.
    pull_days INTEGER,
    pulled_at TIMESTAMP WITH TIME ZONE,
    -- Latest close or alert in the import.
    concluded_at TIMESTAMP WITH TIME ZONE,
    -- False for an import made before history_tickets existed.
    has_ticket_detail BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS public.history_tickets (
    id BIGSERIAL PRIMARY KEY,
    import_id TEXT NOT NULL
        REFERENCES public.history_imports(id) ON DELETE CASCADE,
    -- File order within the import.
    position INTEGER NOT NULL,
    -- The source's own ticket id; null when the export had none.
    ticket_id TEXT,
    source_system TEXT NOT NULL,
    opened_at TIMESTAMP WITH TIME ZONE,
    closed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    disposition_raw TEXT NOT NULL,
    category TEXT NOT NULL
        CONSTRAINT history_tickets_category_check
        CHECK (category IN ('resolved', 'benign', 'false_positive', 'unable_to_resolve')),
    severity TEXT NOT NULL DEFAULT 'unknown'
        CONSTRAINT history_tickets_severity_check
        CHECK (severity IN ('critical', 'high', 'medium', 'low', 'info', 'unknown')),
    rule_id TEXT,
    entity_keys TEXT[] NOT NULL DEFAULT '{}',
    title TEXT NOT NULL DEFAULT '',
    -- Never the notes themselves.
    notes_fingerprint TEXT,
    notes_empty BOOLEAN NOT NULL,
    opened_missing BOOLEAN NOT NULL,
    closed_approximate BOOLEAN NOT NULL,
    bulk_closed BOOLEAN NOT NULL,
    CONSTRAINT history_tickets_import_position_key UNIQUE (import_id, position)
);

-- Every earlier import, as done and without ticket detail.
INSERT INTO public.history_imports (
    id, source_system, status, started_at, finished_at, verdicts_written,
    sightings_written, concluded_at, has_ticket_detail
)
SELECT investigation_id,
       -- Everything before the last ':', as the code reads it.
       coalesce(nullif(regexp_replace(investigation_id, ':[^:]*$', ''), ''), 'file'),
       'done', distilled_at, distilled_at, verdicts_written, sightings_written,
       concluded_at, false
FROM public.episodic_distil_markers
WHERE investigation_kind = 'imported'
ON CONFLICT (id) DO NOTHING;

DELETE FROM public.episodic_distil_markers WHERE investigation_kind = 'imported';
