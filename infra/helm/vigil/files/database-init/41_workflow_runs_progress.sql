-- How far a hunt has got, for the case list (#2202).
--
-- iteration_count is the iteration the hunt last finished and max_cost_usd the
-- ceiling it runs under, extensions included. The agent layer reports both after
-- each iteration. Both stay null for a run that has not (compose, or a hunt before
-- its first iteration), and the case list then reads the cap from the run's trigger
-- context. Existing rows are left blank; nothing is read back out of the ledger.
--
-- A file of its own, not an edit to 12: the Helm init job records each file once
-- it has run. Only the table's owner can add a column, and on Helm that is the
-- chart's user, which this Job runs as.

ALTER TABLE workflow_runs
    ADD COLUMN IF NOT EXISTS iteration_count INTEGER,
    ADD COLUMN IF NOT EXISTS max_cost_usd NUMERIC(10, 4);

COMMENT ON COLUMN workflow_runs.iteration_count IS
    'The iteration a hunt last finished, as the agent layer reported it. Null when it has not.';

COMMENT ON COLUMN workflow_runs.max_cost_usd IS
    'The cost ceiling a hunt runs under, extensions included. Null when the agent layer has not reported it.';
