-- A lock records when and from where it was set (#4176), so the person is told
-- at their next sign-in and a manager can see it. A lock with locked_at set and
-- no locked_until lasts until an Unlock or a reset link.
-- Startup's create_all may already have built the columns from the model.

ALTER TABLE public.users ADD COLUMN IF NOT EXISTS locked_at timestamp without time zone;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS locked_from text;

-- The threshold and the lock are the policy's now: counts start again at the upgrade.
UPDATE public.users SET failed_login_count = 0 WHERE failed_login_count <> 0;
