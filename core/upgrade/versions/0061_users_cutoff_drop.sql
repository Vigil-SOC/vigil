-- Nothing has read users.tokens_valid_after since revocation moved to the
-- sign_ins row (0042). 0004 added it; adopted 0.7.0 and 0.8.0 databases and
-- fresh installs both reach this revision after it, so both lose it here.

ALTER TABLE public.users DROP COLUMN IF EXISTS tokens_valid_after;
