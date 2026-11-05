-- A cold start computes a baseline from history the customer already has. Alerts
-- and tickets stay in these tables: never findings, so a dry run cannot trigger
-- triage or response, and never case_closure_info, since an imported ticket is
-- not a closure this install performed. The transitions between statuses are
-- TRANSITIONS in core/storage/models/cold_start.py.

CREATE TABLE IF NOT EXISTS public.cold_start_runs (
    run_id character varying(36) NOT NULL,
    source text NOT NULL,
    status character varying(16) NOT NULL,
    history_days integer NOT NULL,
    review_days integer NOT NULL,
    judge_limit integer NOT NULL,
    baseline jsonb,
    progress jsonb DEFAULT '{}'::jsonb NOT NULL,
    drops jsonb DEFAULT '{}'::jsonb NOT NULL,
    judging_skipped text,
    error text,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    live_at timestamp with time zone,
    CONSTRAINT cold_start_runs_pkey PRIMARY KEY (run_id),
    CONSTRAINT cold_start_runs_status_check CHECK (
        status IN ('collecting', 'judging', 'ready', 'live', 'failed')
    )
);

-- Two API workers can both ask, so the database holds these two rules.
CREATE UNIQUE INDEX IF NOT EXISTS uq_cold_start_runs_one_active
    ON public.cold_start_runs ((1)) WHERE status IN ('collecting', 'judging');
CREATE UNIQUE INDEX IF NOT EXISTS uq_cold_start_runs_one_live
    ON public.cold_start_runs ((1)) WHERE status = 'live';

CREATE TABLE IF NOT EXISTS public.cold_start_alerts (
    run_id character varying(36) NOT NULL,
    alert_id text NOT NULL,
    source text NOT NULL,
    signature text NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    title text NOT NULL,
    severity text NOT NULL,
    entities jsonb DEFAULT '[]'::jsonb NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    in_review_window boolean DEFAULT false NOT NULL,
    stage1 text,
    rule text,
    reasons jsonb,
    llm jsonb,
    review text,
    reviewed_by text,
    reviewed_at timestamp with time zone,
    CONSTRAINT cold_start_alerts_pkey PRIMARY KEY (run_id, alert_id),
    CONSTRAINT cold_start_alerts_run_fkey FOREIGN KEY (run_id)
        REFERENCES public.cold_start_runs (run_id) ON DELETE CASCADE,
    CONSTRAINT cold_start_alerts_stage1_check CHECK (
        stage1 IN ('escalate', 'abstain', 'dismiss_candidate')
    ),
    CONSTRAINT cold_start_alerts_review_check CHECK (
        review IN ('agree', 'not_benign')
    )
);

CREATE INDEX IF NOT EXISTS idx_cold_start_alerts_review
    ON public.cold_start_alerts USING btree (run_id, in_review_window, stage1);

CREATE TABLE IF NOT EXISTS public.cold_start_outcomes (
    run_id character varying(36) NOT NULL,
    external_id text NOT NULL,
    alert_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    signature text,
    disposition text NOT NULL,
    closed_at timestamp with time zone NOT NULL,
    entities jsonb DEFAULT '[]'::jsonb NOT NULL,
    source_system text,
    CONSTRAINT cold_start_outcomes_pkey PRIMARY KEY (run_id, external_id),
    CONSTRAINT cold_start_outcomes_run_fkey FOREIGN KEY (run_id)
        REFERENCES public.cold_start_runs (run_id) ON DELETE CASCADE,
    CONSTRAINT cold_start_outcomes_disposition_check CHECK (
        disposition IN ('benign_positive', 'false_positive', 'true_positive')
    )
);

COMMENT ON TABLE public.cold_start_runs IS
    'One attempt at computing a baseline from existing history; a live run is the active baseline for new alerts.';
COMMENT ON TABLE public.cold_start_alerts IS
    'A cold start run''s alert snapshots. They never enter findings.';
COMMENT ON TABLE public.cold_start_outcomes IS
    'A cold start run''s closed tickets that passed label filtering. They never enter case_closure_info.';
