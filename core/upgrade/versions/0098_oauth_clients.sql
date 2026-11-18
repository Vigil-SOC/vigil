-- The OAuth authorization server's storage (#4787, epic #4763 D4 as revised).
-- core/auth/oauth_server.py is the only writer. A grant is an mcp_credentials
-- row; there is no grants or tokens table. Naive UTC like the rest of the
-- schema (core/time.py).

-- A client that registered itself (RFC 7591). Public clients only: Vigil
-- issues no client secret, so none is stored. Unused for 30 days and holding
-- no grant, it is purged by the token sweep (services/api/token_sweep.py).
CREATE TABLE IF NOT EXISTS public.oauth_clients (
    client_id VARCHAR(50) PRIMARY KEY,
    name VARCHAR(200),
    redirect_uris JSONB NOT NULL,
    auth_method VARCHAR(40) NOT NULL,
    -- How it came to exist: 'dynamic' today; client-ID metadata documents later.
    source VARCHAR(20) NOT NULL DEFAULT 'dynamic',
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    last_used_at TIMESTAMP WITHOUT TIME ZONE
);

-- An authorization request waiting for consent, then the code it became.
-- Keyed by a SHA-256 of the request id the console URL carries; the code is
-- stored the same way. Short-lived: the sweep deletes it once expired.
CREATE TABLE IF NOT EXISTS public.oauth_pending (
    request_hash CHAR(64) PRIMARY KEY,
    client_id VARCHAR(50) NOT NULL
        REFERENCES public.oauth_clients (client_id) ON DELETE CASCADE,
    redirect_uri TEXT NOT NULL,
    redirect_uri_explicit BOOLEAN NOT NULL,
    state TEXT,
    code_challenge VARCHAR(128) NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT NOT NULL,
    -- Who approved, and the code issued; both null until consent.
    user_id VARCHAR(50) REFERENCES public.users (user_id) ON DELETE CASCADE,
    code_hash CHAR(64) UNIQUE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_oauth_pending_expires ON public.oauth_pending (expires_at);

-- A grant: the credential an OAuth client holds for one person. Null on a
-- personal API token. The refresh hashes are SHA-256; the previous one is kept
-- to detect a reused refresh token.
ALTER TABLE public.mcp_credentials
    ADD COLUMN IF NOT EXISTS oauth_client_id VARCHAR(50)
        REFERENCES public.oauth_clients (client_id),
    ADD COLUMN IF NOT EXISTS access_expires_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS refresh_hash CHAR(64),
    ADD COLUMN IF NOT EXISTS previous_refresh_hash CHAR(64);

CREATE UNIQUE INDEX IF NOT EXISTS uq_mcp_credentials_refresh_hash
    ON public.mcp_credentials (refresh_hash) WHERE refresh_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mcp_credentials_previous_refresh_hash
    ON public.mcp_credentials (previous_refresh_hash) WHERE previous_refresh_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mcp_credentials_oauth_client
    ON public.mcp_credentials (oauth_client_id) WHERE oauth_client_id IS NOT NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.oauth_clients TO vigil_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.oauth_pending TO vigil_app;
