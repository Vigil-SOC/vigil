ALTER TABLE public.federation_sources
    ADD COLUMN IF NOT EXISTS last_error_kind text
    CONSTRAINT federation_sources_last_error_kind_check
    CHECK (last_error_kind IN ('auth','network','config','quota','upstream','unknown'));
