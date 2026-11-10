-- A run records the sign-in it started from (#4173), so the runs a compromise
-- ending must stop can be found without their tokens. Existing runs have none.
-- Startup's create_all may already have built the column from the model.

ALTER TABLE public.workflow_runs ADD COLUMN IF NOT EXISTS sign_in_id text;
CREATE INDEX IF NOT EXISTS idx_workflow_runs_sign_in
    ON public.workflow_runs USING btree (sign_in_id) WHERE (sign_in_id IS NOT NULL);
