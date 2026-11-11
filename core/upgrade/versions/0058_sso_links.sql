-- Single sign-on links (#4284, epic #4237): which account a provider's subject
-- signs in as. One (issuer, subject) names one account. The same foreign key as
-- sign_ins and security_keys, so deleting an account (a mark on the principal)
-- is not blocked by its link. Startup's create_all may already have built it.

CREATE TABLE IF NOT EXISTS public.sso_links (
    link_id character varying(50) NOT NULL,
    principal_id text NOT NULL,
    issuer text NOT NULL,
    subject text NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT sso_links_pkey PRIMARY KEY (link_id),
    CONSTRAINT sso_links_principal_id_fkey FOREIGN KEY (principal_id)
        REFERENCES public.principals (principal_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_sso_links_principal
    ON public.sso_links USING btree (principal_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_sso_links_subject
    ON public.sso_links USING btree (issuer, subject);
