-- The install's upgrade time, written once and never overwritten on a re-run.
-- Sign-ins rewrite each bcrypt hash as they happen; this row marks the start
-- of that window (the later expiry of bcrypt hashes counts from it).

INSERT INTO public.system_config (key, value, description, config_type)
VALUES (
    'auth.password_scheme_upgraded_at',
    to_jsonb(to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
    'When this install moved to PBKDF2 password hashes (#4174)',
    'auth'
)
ON CONFLICT (key) DO NOTHING;
