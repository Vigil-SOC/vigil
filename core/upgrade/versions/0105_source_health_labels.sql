-- Sender labels seen on push sources (#4870, epic #4866). A bounded map of
-- label -> last-seen naive UTC time, capped by the writer. Information only:
-- judge() does not read it. The table grant from 0104_source_health already
-- covers the new column; restated so a role created the same way keeps it.
ALTER TABLE public.source_health
    ADD COLUMN IF NOT EXISTS labels JSONB NOT NULL DEFAULT '{}'::jsonb;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.source_health TO vigil_app;
