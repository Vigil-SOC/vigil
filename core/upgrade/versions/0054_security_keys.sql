-- Security keys (#4234, epic #4164): a person's registered WebAuthn credentials.
-- Only what checking an assertion needs. Keys belong to the principal's account,
-- with the same foreign key as sign_ins, so deleting an account (a mark on the
-- principal) is not blocked by them. last_challenge is the replay guard for an
-- authenticator whose signature counter never moves.
-- Startup's create_all may already have built the table from the model.

CREATE TABLE IF NOT EXISTS public.security_keys (
    key_id character varying(50) NOT NULL,
    principal_id text NOT NULL,
    credential_id bytea NOT NULL,
    public_key bytea NOT NULL,
    algorithm integer NOT NULL,
    sign_count bigint DEFAULT 0 NOT NULL,
    name character varying(100) NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    last_used_at timestamp without time zone,
    last_challenge bytea,
    CONSTRAINT security_keys_pkey PRIMARY KEY (key_id),
    CONSTRAINT security_keys_principal_id_fkey FOREIGN KEY (principal_id)
        REFERENCES public.principals (principal_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_security_keys_principal
    ON public.security_keys USING btree (principal_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_security_keys_credential
    ON public.security_keys USING btree (credential_id);
