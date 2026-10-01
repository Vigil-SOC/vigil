-- The agent-layer terminal beside the three-value status (#1272).
--
-- status stays completed | failed | cancelled, which is what the console and
-- the finished-run counter already filter on. outcome is the raw terminal the
-- bridge was given (completed, budget_exhausted, aborted, abandoned, failed)
-- and reason is why. Both stay null when this side finalized the run itself:
-- an operator cancel, or a queue that refused the job. Existing rows are left
-- blank; nothing is read back out of the ledger.
--
-- A file of its own, not an edit to 12: the Helm init job records each file
-- once it has run, so a change to 12 would never reach a cluster that already
-- applied it. Only the table's owner can add a column, and on Helm that is
-- the chart's user, which this Job runs as.

ALTER TABLE workflow_runs
    ADD COLUMN IF NOT EXISTS outcome TEXT,
    ADD COLUMN IF NOT EXISTS reason TEXT;

COMMENT ON COLUMN workflow_runs.outcome IS
    'The agent-layer terminal, when the bridge wrote it. Null when this side finalized the run itself.';

COMMENT ON COLUMN workflow_runs.reason IS
    'Why the agent layer ended the run. Not the error column: that stays the crash the console renders in red.';
