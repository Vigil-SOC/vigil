-- Directory sync (#4423). The provider's externalId sits beside the sign-in
-- subject. deactivation_route is written only by deactivate_account.
-- scim_version is the user's If-Match token.

ALTER TABLE public.sso_links
    ADD COLUMN IF NOT EXISTS directory_id text,
    ADD COLUMN IF NOT EXISTS managed boolean NOT NULL DEFAULT false;

CREATE UNIQUE INDEX IF NOT EXISTS uq_sso_links_directory
    ON public.sso_links (issuer, directory_id)
    WHERE directory_id IS NOT NULL;

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS deactivation_route text,
    ADD COLUMN IF NOT EXISTS deactivated_at timestamp without time zone,
    ADD COLUMN IF NOT EXISTS scim_version integer NOT NULL DEFAULT 1;

ALTER TABLE public.users DROP CONSTRAINT IF EXISTS users_deactivation_route_check;
ALTER TABLE public.users
    ADD CONSTRAINT users_deactivation_route_check
    CHECK (
        deactivation_route IS NULL
        OR deactivation_route IN ('sync', 'person', 'dormancy')
    );
