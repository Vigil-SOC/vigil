-- Day 1 Report (#4875). A run's start is its own row so a start with no
-- record can be shown. The record is inserted once the run finishes.
-- Naive UTC like the rest of the schema (core/time.py).
CREATE TABLE public.report_runs (
    run_id VARCHAR(36) PRIMARY KEY,
    started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    trigger TEXT NOT NULL
        CONSTRAINT report_runs_trigger_check
        CHECK (trigger IN ('readiness', 'fallback', 'on_demand')),
    actor_id TEXT
);

CREATE INDEX report_runs_started_at_idx ON public.report_runs (started_at DESC);

CREATE TABLE public.report_records (
    record_id VARCHAR(36) PRIMARY KEY,
    run_id VARCHAR(36) NOT NULL UNIQUE REFERENCES public.report_runs (run_id),
    recorded_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    trigger TEXT NOT NULL
        CONSTRAINT report_records_trigger_check
        CHECK (trigger IN ('readiness', 'fallback', 'on_demand')),
    actor_id TEXT,
    vigil_version TEXT NOT NULL,
    modules JSONB NOT NULL,
    checks JSONB NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    preface TEXT
);

CREATE INDEX report_records_recorded_at_idx ON public.report_records (recorded_at DESC);

-- Default privileges grant vigil_app UPDATE and DELETE. Insert-only, like
-- agent_events: the role may add a row and read it back.
GRANT SELECT, INSERT ON TABLE public.report_runs TO vigil_app;
GRANT SELECT, INSERT ON TABLE public.report_records TO vigil_app;
REVOKE UPDATE, DELETE, TRUNCATE ON TABLE public.report_runs FROM vigil_app;
REVOKE UPDATE, DELETE, TRUNCATE ON TABLE public.report_records FROM vigil_app;
