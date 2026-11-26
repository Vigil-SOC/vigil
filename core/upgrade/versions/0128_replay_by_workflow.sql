-- The replay's counts per workflow id (#5223). A row written before this reads
-- with an empty map.
ALTER TABLE public.investigation_replay
    ADD COLUMN by_workflow jsonb NOT NULL DEFAULT '{}'::jsonb;
