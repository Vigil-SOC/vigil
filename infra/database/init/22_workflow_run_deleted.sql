-- Hiding a run from History without losing it. A run's ledger is the only account
-- of what an agent did and why, so History's delete marks the row rather than
-- dropping it: agent_events, phases and approvals stay readable by run_id.

ALTER TABLE workflow_runs
    ADD COLUMN IF NOT EXISTS deleted_at timestamp;

COMMENT ON COLUMN workflow_runs.deleted_at IS
    'When an operator removed this run from the listings. Null is the live case. The row and its ledger are kept: a deleted run is one nobody wants to see, not one nobody may audit.';

-- Listings read one playbook's live runs newest first, and the partial index keeps
-- deleted rows out of that scan rather than filtering them after it.
CREATE INDEX IF NOT EXISTS idx_workflow_runs_live
    ON workflow_runs (workflow_id, started_at DESC)
    WHERE deleted_at IS NULL;
