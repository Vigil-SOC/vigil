-- The key ring (#4180): one random key per purpose, each wrapped under a key
-- derived from JWT_SECRET_KEY, which this migration never sees (the upgrade Job
-- has no app Secret). The table starts empty; the first app start adopts the
-- keys derived today as version 1 and writes version 2 beside them.
-- wrapped is NULL once a signing key's overlap has ended.

CREATE TABLE public.auth_keys (
    purpose text NOT NULL,
    version integer NOT NULL,
    state text NOT NULL
        CHECK (state IN ('current', 'verify_only', 'decrypt_only', 'retired')),
    wrapped text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    verify_until timestamp with time zone,
    retired_at timestamp with time zone,
    PRIMARY KEY (purpose, version)
);

-- One current key per purpose, even when two rotations race.
CREATE UNIQUE INDEX auth_keys_one_current ON public.auth_keys (purpose)
    WHERE state = 'current';

COMMENT ON TABLE public.auth_keys IS
    'Wrapped signing and encryption keys, one current per purpose (#4180).';

REVOKE ALL ON TABLE public.auth_keys FROM vigil_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.auth_keys TO vigil_app;
