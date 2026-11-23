-- What a run's report stands on (#4962), from its terminal. Nullable with no
-- default: null is "the writer did not say", never false, so a run that ended
-- before this step ranks as unread rather than as a report that does not stand.
ALTER TABLE public.workflow_runs
    ADD COLUMN IF NOT EXISTS capped BOOLEAN,
    ADD COLUMN IF NOT EXISTS citation_count INTEGER,
    ADD COLUMN IF NOT EXISTS grounded BOOLEAN;
