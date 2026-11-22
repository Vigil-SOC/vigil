-- Federation poll counts under the existing one-hour dropped_window_start
-- anchor (#4940). Nullable with no default: null is "not counted yet", so the
-- first successful poll after upgrade restarts the window instead of mixing
-- pre-upgrade drops with the new counts. Other writers leave them alone.
ALTER TABLE public.source_health
    ADD COLUMN IF NOT EXISTS returned_window INTEGER,
    ADD COLUMN IF NOT EXISTS stored_window INTEGER,
    ADD COLUMN IF NOT EXISTS filtered_window INTEGER;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.source_health TO vigil_app;
