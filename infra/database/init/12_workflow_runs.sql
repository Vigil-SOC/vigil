-- Workflow Runs — execution history + audit trail (#127)
--
-- Workflows used to execute and vanish: no trace of who ran what, no way to
-- answer "show me the last run of incident-response on case X", no way to
-- debug "why did this run fail?". This migration adds two tables:
--
--   workflow_runs         — one row per execute_workflow invocation
--   workflow_run_phases   — reserved for phase-by-phase execution (#128);
--                           the table is created now so schema ships with
--                           the audit story, but rows are only written
--                           once phase-level execution lands.
--
-- Idempotent via ``CREATE TABLE IF NOT EXISTS``; safe to re-run against
-- an existing DB.

CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id              VARCHAR(80)  PRIMARY KEY,           -- wfr-YYYYMMDD-<uuid8>
    workflow_id         TEXT         NOT NULL,
    workflow_version    INTEGER,                            -- from custom_workflows; null for file-based
    workflow_source     VARCHAR(16)  NOT NULL DEFAULT 'file', -- "file" | "custom"
    workflow_name       TEXT         NOT NULL,              -- denormalised; file-based workflows aren't in DB
    status              VARCHAR(16)  NOT NULL
        CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
    triggered_by        VARCHAR(100),                       -- user id, "daemon", "api", ...
    trigger_context     JSONB        NOT NULL DEFAULT '{}'::jsonb, -- {finding_id, case_id, hypothesis, ...}
    started_at          TIMESTAMP    NOT NULL DEFAULT NOW(),
    finished_at         TIMESTAMP,
    duration_ms         INTEGER,
    total_cost_usd      NUMERIC(10, 4) NOT NULL DEFAULT 0,
    result_summary      TEXT,                               -- final text output from the workflow
    skill_tools_available JSONB      NOT NULL DEFAULT '[]'::jsonb, -- snapshot of active skill tools at run-start
    error               TEXT
);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow_id  ON workflow_runs(workflow_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_status       ON workflow_runs(status)
    WHERE status IN ('running', 'failed');
CREATE INDEX IF NOT EXISTS idx_workflow_runs_started_at   ON workflow_runs(started_at DESC);


CREATE TABLE IF NOT EXISTS workflow_run_phases (
    run_id              VARCHAR(80)  NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    phase_id            TEXT         NOT NULL,
    phase_order         INTEGER      NOT NULL,
    agent_id            TEXT         NOT NULL,
    status              VARCHAR(16)  NOT NULL
        CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped', 'pending_approval')),
    started_at          TIMESTAMP,
    finished_at         TIMESTAMP,
    duration_ms         INTEGER,
    input_context       JSONB        NOT NULL DEFAULT '{}'::jsonb, -- structured output from prior phase
    output              JSONB        NOT NULL DEFAULT '{}'::jsonb, -- structured output this phase produced
    approval_state      VARCHAR(16),                         -- null | pending | approved | rejected
    cost_usd            NUMERIC(10, 4) NOT NULL DEFAULT 0,
    error               TEXT,
    PRIMARY KEY (run_id, phase_id)
);

CREATE INDEX IF NOT EXISTS idx_workflow_run_phases_run_id ON workflow_run_phases(run_id, phase_order);


COMMENT ON TABLE  workflow_runs IS 'Per-invocation record of workflow execution — history + audit trail (#127).';
COMMENT ON COLUMN workflow_runs.run_id IS 'Format: wfr-YYYYMMDD-<uuid8>.';
COMMENT ON COLUMN workflow_runs.workflow_source IS 'file = workflows/*/WORKFLOW.md; custom = DB-backed row in custom_workflows.';
COMMENT ON COLUMN workflow_runs.skill_tools_available IS 'Snapshot of active skill_* tool names at run-start so rerun results are reproducible when the skill set changes.';

COMMENT ON TABLE  workflow_run_phases IS 'Reserved for phase-by-phase workflow execution (#128). Rows are only written once phase-level execution lands; until then, runs surface at the workflow_runs level only.';


GRANT SELECT, INSERT, UPDATE, DELETE ON workflow_runs        TO deeptempo;
GRANT SELECT, INSERT, UPDATE, DELETE ON workflow_run_phases  TO deeptempo;
