-- Accounts idle past the policy's dormancy period are deactivated (#4176). The
-- clock never reaches back before the upgrade, so no account is deactivated for
-- time that passed before the rule existed; dormancy_warned_at is when its
-- holder and managers were last warned, dormancy_held_at when managers were last
-- told the account was kept as the last role manager.
-- Startup's create_all may already have built the columns from the model.

ALTER TABLE public.users ADD COLUMN IF NOT EXISTS dormancy_floor timestamp without time zone;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS dormancy_warned_at timestamp without time zone;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS dormancy_held_at timestamp without time zone;

UPDATE public.users SET dormancy_floor = now() AT TIME ZONE 'utc' WHERE dormancy_floor IS NULL;
