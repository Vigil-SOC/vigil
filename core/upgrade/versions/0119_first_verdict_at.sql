-- The first time triage recorded a verdict (#4999). Null until then, including
-- every finding triaged before this revision. Writers set it once.
ALTER TABLE public.findings
    ADD COLUMN IF NOT EXISTS first_verdict_at TIMESTAMP WITHOUT TIME ZONE;
