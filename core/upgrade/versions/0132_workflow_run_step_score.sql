-- A dismissal's step score and what the close check did (#5275). Nullable, no
-- default: a run that dismissed nothing, or ended before this step, has none.
ALTER TABLE public.workflow_runs
    ADD COLUMN IF NOT EXISTS step_score JSONB;
