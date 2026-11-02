CREATE INDEX IF NOT EXISTS idx_workflow_runs_case_id ON public.workflow_runs ((trigger_context ->> 'case_id')) WHERE deleted_at IS NULL;
