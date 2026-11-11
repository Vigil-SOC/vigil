-- Service accounts for integrations (#4224): token-only, so no password; what
-- each is for; and the person who answers for it, by principal id.
-- Startup's create_all may already have built the columns from the model.

ALTER TABLE public.users ALTER COLUMN password_hash DROP NOT NULL;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS purpose text;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS accountable_principal_id text
    REFERENCES public.principals(principal_id) ON DELETE RESTRICT;
