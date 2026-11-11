-- When each key was last wrapped under JWT_SECRET_KEY (#4181). The oldest
-- wrapped_at among the keys still held is the wrapping secret's age: restore
-- re-wrapping a ring and a rotation of the secret set it, a plain rotation of
-- one purpose does not move the rest. Existing rows were wrapped when written.

ALTER TABLE public.auth_keys
    ADD COLUMN IF NOT EXISTS wrapped_at timestamp with time zone;

UPDATE public.auth_keys SET wrapped_at = created_at WHERE wrapped_at IS NULL;

ALTER TABLE public.auth_keys
    ALTER COLUMN wrapped_at SET DEFAULT now(),
    ALTER COLUMN wrapped_at SET NOT NULL;
