-- Breached passwords an administrator imported (#4175): one row per lowercased
-- entry. Imports only add, so the app role may read and insert and nothing more.
-- Startup's create_all may already have built the table from the model.

CREATE TABLE IF NOT EXISTS public.breached_passwords (
    entry text NOT NULL,
    added_at timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT breached_passwords_pkey PRIMARY KEY (entry)
);

REVOKE ALL ON TABLE public.breached_passwords FROM vigil_app;
GRANT SELECT, INSERT ON TABLE public.breached_passwords TO vigil_app;
