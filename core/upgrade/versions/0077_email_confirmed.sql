-- A changed email is confirmed by a 24-hour link (kind ``email``). Addresses
-- already on file count as confirmed: only a person's own later change leaves
-- one unconfirmed. Startup's create_all never changes a check, so it is
-- replaced here.

ALTER TABLE public.auth_links DROP CONSTRAINT IF EXISTS auth_links_kind_check;
ALTER TABLE public.auth_links
    ADD CONSTRAINT auth_links_kind_check
    CHECK (kind IN ('invite', 'reset', 'email'));

CREATE UNIQUE INDEX IF NOT EXISTS uq_auth_links_open_email
    ON public.auth_links (principal_id)
    WHERE kind = 'email' AND used_at IS NULL AND revoked_at IS NULL;

UPDATE public.users SET is_verified = true WHERE NOT is_verified;
