-- Approval Actions — pending human-in-the-loop approvals (#128)
--
-- Before this migration, ApprovalService persisted pending actions to
-- ``data/pending_actions.json``. That worked for the daemon's single-
-- process orchestrator but was invisible to the API/UI and had no FK
-- into workflow runs. Workflow phase-level approval gating (#128)
-- needs a queryable, joinable surface with a direct link back to the
-- paused workflow_run / phase.
--
-- This migration creates ``approval_actions`` (the DB home) and
-- relaxes ``workflow_runs.status`` to include ``'paused'`` so a run
-- blocked on approval has a dedicated terminal-ish state while it
-- waits for the analyst.
--
-- Idempotent via IF NOT EXISTS / conditional constraint drops; safe
-- to re-run.

CREATE TABLE IF NOT EXISTS approval_actions (
    action_id           VARCHAR(80)  PRIMARY KEY,                  -- action-YYYYMMDD-HHMMSS-ffffff
    action_type         VARCHAR(40)  NOT NULL,                     -- ActionType enum value
    title               TEXT         NOT NULL,
    description         TEXT         NOT NULL,
    target              TEXT         NOT NULL,                     -- IP, hostname, username, run_id, etc.
    confidence          NUMERIC(4, 3) NOT NULL DEFAULT 0,          -- 0.000 - 1.000
    reason              TEXT         NOT NULL,
    evidence            JSONB        NOT NULL DEFAULT '[]'::jsonb, -- finding IDs / refs
    created_at          TIMESTAMP    NOT NULL DEFAULT NOW(),
    created_by          VARCHAR(100) NOT NULL,
    requires_approval   BOOLEAN      NOT NULL DEFAULT TRUE,
    status              VARCHAR(16)  NOT NULL
        CHECK (status IN ('pending', 'approved', 'rejected', 'executed', 'failed')),
    approved_at         TIMESTAMP,
    approved_by         VARCHAR(100),
    executed_at         TIMESTAMP,
    execution_result    JSONB,
    rejection_reason    TEXT,
    parameters          JSONB        NOT NULL DEFAULT '{}'::jsonb,
    -- Workflow linkage (#128): null for non-workflow approvals (e.g.
    -- daemon-triggered containment actions).
    workflow_run_id     VARCHAR(80)  REFERENCES workflow_runs(run_id) ON DELETE SET NULL,
    workflow_phase_id   TEXT
);

CREATE INDEX IF NOT EXISTS idx_approval_actions_status_created
    ON approval_actions(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_approval_actions_workflow_run
    ON approval_actions(workflow_run_id)
    WHERE workflow_run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_approval_actions_pending
    ON approval_actions(created_at DESC)
    WHERE status = 'pending' AND requires_approval = TRUE;


-- Extend workflow_runs.status to include 'paused'. A workflow run
-- enters 'paused' when a phase with approval_required=TRUE blocks
-- the loop; resume_workflow() transitions it back to 'running' on
-- approve or 'cancelled' on reject.
ALTER TABLE workflow_runs DROP CONSTRAINT IF EXISTS workflow_runs_status_check;
ALTER TABLE workflow_runs ADD CONSTRAINT workflow_runs_status_check
    CHECK (status IN ('running', 'paused', 'completed', 'failed', 'cancelled'));


COMMENT ON TABLE  approval_actions IS 'Pending human-in-the-loop approvals. Workflow phase approvals link back via workflow_run_id + workflow_phase_id (#128).';
COMMENT ON COLUMN approval_actions.workflow_run_id IS 'FK to workflow_runs — null for non-workflow approvals (e.g. daemon containment actions).';
COMMENT ON COLUMN approval_actions.workflow_phase_id IS 'The phase_id inside the paused workflow run this approval gates.';


GRANT SELECT, INSERT, UPDATE, DELETE ON approval_actions TO deeptempo;
