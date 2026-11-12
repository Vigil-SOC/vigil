-- Restricted cases (#4354). Who may read a restricted case: principal ids, and
-- (once teams can be named) team ids. NULL or empty is open: every case already
-- stored stays open.

ALTER TABLE public.cases ADD COLUMN reader_principal_ids text[];
ALTER TABLE public.cases ADD COLUMN reader_team_ids character varying(50)[];
