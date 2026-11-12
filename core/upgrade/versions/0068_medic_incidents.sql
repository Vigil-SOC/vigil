-- Open count and sent incident notices for the Medic watch (#4366).
-- The watch in core/platform/medic_incidents.py is the only writer.

ALTER TABLE public.medic_watch_state
    ADD COLUMN IF NOT EXISTS open_count INTEGER,
    ADD COLUMN IF NOT EXISTS open_count_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS summary_started_at TEXT;

ALTER TABLE public.medic_watch_state
    DROP CONSTRAINT IF EXISTS medic_watch_state_open_count;
ALTER TABLE public.medic_watch_state
    ADD CONSTRAINT medic_watch_state_open_count
    CHECK (open_count IS NULL OR open_count >= 0);

ALTER TABLE public.medic_watch_state
    DROP CONSTRAINT IF EXISTS medic_watch_state_summary_start;
ALTER TABLE public.medic_watch_state
    ADD CONSTRAINT medic_watch_state_summary_start
    CHECK (summary_started_at IS NULL OR char_length(summary_started_at) <= 128);

CREATE TABLE IF NOT EXISTS public.medic_incident_notice (
    incident_id TEXT NOT NULL,
    reopen_count INTEGER NOT NULL,
    event TEXT NOT NULL,
    summary TEXT,
    fix TEXT,
    PRIMARY KEY (incident_id, reopen_count, event),
    CONSTRAINT medic_incident_notice_id CHECK (
        incident_id ~ '^inc_[0-9A-Za-z]{8,32}$'
    ),
    CONSTRAINT medic_incident_notice_reopen CHECK (reopen_count >= 0),
    CONSTRAINT medic_incident_notice_event CHECK (
        event IN ('opened', 'cleared', 'summary')
    ),
    CONSTRAINT medic_incident_notice_summary CHECK (
        summary IS NULL OR char_length(summary) <= 200
    ),
    CONSTRAINT medic_incident_notice_fix CHECK (
        fix IS NULL OR char_length(fix) <= 1000
    )
);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.medic_incident_notice TO vigil_app;
