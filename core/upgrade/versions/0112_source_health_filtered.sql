-- Cumulative findings a source's severity floor skipped (#4891). Not a drop:
-- the judge does not read it, and a reset keeps it, as it keeps dropped_total.
ALTER TABLE public.source_health
    ADD COLUMN IF NOT EXISTS filtered_total INTEGER NOT NULL DEFAULT 0;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.source_health TO vigil_app;
