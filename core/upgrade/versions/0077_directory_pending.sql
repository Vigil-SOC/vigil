-- A SCIM create whose username or email is already in use (#4507). One row per
-- externalId, refreshed on each retry, until an admin links it to an account.

CREATE TABLE public.directory_pending (
    pending_id text NOT NULL,
    external_id text NOT NULL,
    username text NOT NULL,
    email text NOT NULL,
    display_name text,
    received_at timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT directory_pending_pkey PRIMARY KEY (pending_id)
);

CREATE UNIQUE INDEX uq_directory_pending_external
    ON public.directory_pending USING btree (external_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.directory_pending TO vigil_app;
