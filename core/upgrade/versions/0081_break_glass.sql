-- When the host marked the account break-glass; NULL for every other account.
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS break_glass_at timestamp without time zone;
