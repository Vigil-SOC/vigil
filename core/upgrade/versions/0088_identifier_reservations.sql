-- A username or email an erased person held, kept for two years as an
-- HMAC-SHA-256 of its folded form (core/auth/identity.py) and nothing else:
-- no value, no kind and no person. key_version names the key-ring "identifier"
-- key that made the hash. Naive UTC like the rest of the schema (core/time.py).

CREATE TABLE IF NOT EXISTS public.identifier_reservations (
    hash TEXT PRIMARY KEY,
    key_version INTEGER NOT NULL,
    reserved_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.identifier_reservations TO vigil_app;
