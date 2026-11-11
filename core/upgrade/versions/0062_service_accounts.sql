-- A service account created through the API has no password and signs in by token
-- only. It records its purpose and the person accountable for it, by principal id:
-- an erasure rewrites names but keeps principal ids.

ALTER TABLE public.users
    ALTER COLUMN password_hash DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS purpose text,
    ADD COLUMN IF NOT EXISTS accountable_principal_id text
        REFERENCES public.principals(principal_id) ON DELETE RESTRICT;
