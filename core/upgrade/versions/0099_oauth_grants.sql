-- What an OAuth grant and its approval are bound to (#4788).

-- The resource (RFC 8707) a grant's tokens are for: the /mcp URL the consented
-- request named. The gate refuses a token whose resource is not this
-- instance's. Null on a personal API token.
ALTER TABLE public.mcp_credentials
    ADD COLUMN IF NOT EXISTS resource TEXT;

-- The sign-in that approved a request. Its code is exchanged only while that
-- sign-in is open, so ending a person's sign-ins (deactivation, a suspected
-- compromise) also ends a code they approved and the client has not used yet.
ALTER TABLE public.oauth_pending
    ADD COLUMN IF NOT EXISTS sign_in_id VARCHAR(50);
