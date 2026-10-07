-- The index the WorkflowRun model declares on who started a run (#1245).
--
-- 12_workflow_runs.sql never built it, and create_all adds no index to a table
-- that already exists. Only the table's owner can add one, and on Helm that is
-- the chart's user, which this Job runs as; vigil_app cannot. Without it,
-- scripts/migrate_schema.py run as vigil_app skips the step and exits 1 until
-- someone runs it again as the chart's user.
--
-- A file of its own, not an edit to 12: the Helm init job records each file
-- once it has run, so a change to 12 would never reach a cluster that already
-- applied it.
CREATE INDEX IF NOT EXISTS idx_workflow_runs_triggered_by
    ON workflow_runs (triggered_by, started_at);
