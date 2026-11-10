-- Single-use links (#4226). Only the SHA-256 of the link's random value is
-- kept. A link is used up by the submission that completes it (used_at), and
-- cancelled by a newer one or a revoke (revoked_at). Invites carry the role and
-- the inviter; the account is created at acceptance and named in user_id.
-- One pending invite per email; an expired one stays pending until replaced.
-- Startup's create_all may already have built the table from the model.

CREATE TABLE IF NOT EXISTS public.account_links (
    link_id character varying(50) PRIMARY KEY,
    kind character varying(10) NOT NULL,
    token_hash character(64) NOT NULL,
    email character varying(200),
    user_id character varying(50),
    role_id character varying(50),
    invited_by text,
    channel character varying(10),
    issued_at timestamp without time zone DEFAULT now() NOT NULL,
    expires_at timestamp without time zone NOT NULL,
    used_at timestamp without time zone,
    revoked_at timestamp without time zone,
    expiry_recorded_at timestamp without time zone,
    attempts integer DEFAULT 0 NOT NULL,
    CONSTRAINT account_links_kind_check CHECK (kind IN ('invite', 'reset')),
    CONSTRAINT account_links_channel_check CHECK (channel IN ('email', 'copy'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_account_links_token
    ON public.account_links (token_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_account_links_pending_invite
    ON public.account_links (lower(email))
    WHERE kind = 'invite' AND used_at IS NULL AND revoked_at IS NULL;

REVOKE ALL ON TABLE public.account_links FROM vigil_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.account_links TO vigil_app;
