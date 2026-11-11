-- Single-use links (#4226, epic #4219): an invite to set up an account. The
-- kind column is shared with the recovery sibling's resets. Only the SHA-256 of
-- the random value is stored. Startup's create_all may already have built it.

CREATE TABLE IF NOT EXISTS public.auth_links (
    link_id character varying(50) NOT NULL,
    kind character varying(10) NOT NULL,
    token_hash character varying(64) NOT NULL,
    email text,
    principal_id text,
    role_id character varying(50),
    invited_by text,
    channel character varying(10),
    issued_at timestamp without time zone DEFAULT now() NOT NULL,
    expires_at timestamp without time zone NOT NULL,
    used_at timestamp without time zone,
    revoked_at timestamp without time zone,
    expired_noted_at timestamp without time zone,
    attempts integer DEFAULT 0 NOT NULL,
    CONSTRAINT auth_links_pkey PRIMARY KEY (link_id),
    CONSTRAINT auth_links_kind_check CHECK (kind IN ('invite', 'reset')),
    CONSTRAINT auth_links_principal_id_fkey FOREIGN KEY (principal_id)
        REFERENCES public.principals (principal_id) ON DELETE RESTRICT,
    CONSTRAINT auth_links_invited_by_fkey FOREIGN KEY (invited_by)
        REFERENCES public.principals (principal_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_auth_links_token_hash
    ON public.auth_links USING btree (token_hash);
CREATE UNIQUE INDEX IF NOT EXISTS uq_auth_links_open_invite
    ON public.auth_links USING btree (lower(email))
    WHERE kind = 'invite' AND used_at IS NULL AND revoked_at IS NULL;
