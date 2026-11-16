-- Pushed groups (#4424). external_id is immutable. Members are principal
-- ids of accounts that already exist. The rows grant nothing.

CREATE TABLE public.directory_groups (
    group_id text NOT NULL,
    external_id text NOT NULL,
    display_name text NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT directory_groups_pkey PRIMARY KEY (group_id)
);

CREATE UNIQUE INDEX uq_directory_groups_external
    ON public.directory_groups USING btree (external_id);

CREATE TABLE public.directory_group_members (
    group_id text NOT NULL,
    principal_id text NOT NULL,
    CONSTRAINT directory_group_members_pkey PRIMARY KEY (group_id, principal_id),
    CONSTRAINT directory_group_members_group_id_fkey FOREIGN KEY (group_id)
        REFERENCES public.directory_groups (group_id) ON DELETE CASCADE,
    CONSTRAINT directory_group_members_principal_id_fkey FOREIGN KEY (principal_id)
        REFERENCES public.principals (principal_id) ON DELETE RESTRICT
);

CREATE INDEX idx_directory_group_members_principal
    ON public.directory_group_members USING btree (principal_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.directory_groups TO vigil_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.directory_group_members TO vigil_app;
