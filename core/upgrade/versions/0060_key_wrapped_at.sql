-- When each ring key was last wrapped under JWT_SECRET_KEY (#4181). The oldest
-- non-retired row says how long the current wrapping secret has been in use;
-- re-wrapping the ring (a restore from a paired backup, or a rotation of the
-- secret) sets it again. Existing rows start at their creation time.

ALTER TABLE public.auth_keys
    ADD COLUMN IF NOT EXISTS wrapped_at timestamp with time zone;
UPDATE public.auth_keys SET wrapped_at = created_at WHERE wrapped_at IS NULL;
ALTER TABLE public.auth_keys ALTER COLUMN wrapped_at SET DEFAULT now();
ALTER TABLE public.auth_keys ALTER COLUMN wrapped_at SET NOT NULL;
