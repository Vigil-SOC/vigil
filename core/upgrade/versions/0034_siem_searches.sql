-- A row per search a check sent a SIEM (#3741), so every Vigil process counts
-- one max_searches_per_hour window per integration. search_key names what was
-- searched (a Kibana Sees group) when the caller orders its work by it; the
-- latest row per key is kept past the hour for that order, other rows are
-- pruned once they leave the window.
CREATE TABLE IF NOT EXISTS public.siem_searches (
    id bigserial NOT NULL,
    integration_id text NOT NULL,
    search_key text,
    searched_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT siem_searches_pkey PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_siem_searches_window
    ON public.siem_searches USING btree (integration_id, searched_at);

REVOKE ALL ON TABLE public.siem_searches FROM vigil_app;
GRANT SELECT, INSERT, DELETE ON TABLE public.siem_searches TO vigil_app;
GRANT USAGE ON SEQUENCE public.siem_searches_id_seq TO vigil_app;
