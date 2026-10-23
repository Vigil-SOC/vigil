-- Principal ids beside the working actor text (#2763). The text columns stay:
-- a row written before this revision still says who it named. The new columns
-- are nullable and unenforced so a writer that does not yet know the id leaves
-- them empty rather than inventing one.

ALTER TABLE public.workflow_runs
    ADD COLUMN IF NOT EXISTS triggered_by_principal_id text;

ALTER TABLE public.approval_actions
    ADD COLUMN IF NOT EXISTS created_by_principal_id text,
    ADD COLUMN IF NOT EXISTS approved_by_principal_id text;

ALTER TABLE public.agent_directives
    ADD COLUMN IF NOT EXISTS actor_principal_id text;

ALTER TABLE public.episodic_read_log
    ADD COLUMN IF NOT EXISTS caller_principal_id text;

COMMENT ON COLUMN public.workflow_runs.triggered_by_principal_id IS
    'Principal of triggered_by. The text column stays.';
COMMENT ON COLUMN public.approval_actions.created_by_principal_id IS
    'Principal of created_by. The text column stays.';
COMMENT ON COLUMN public.approval_actions.approved_by_principal_id IS
    'Principal of approved_by. The text column stays.';
COMMENT ON COLUMN public.agent_directives.actor_principal_id IS
    'Principal of actor. The text column stays.';
COMMENT ON COLUMN public.episodic_read_log.caller_principal_id IS
    'Principal of caller_id. The text column stays, and the 90-day purge still drops the row.';
