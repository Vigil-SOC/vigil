-- How a verdict was reached: 'static' or 'llm:<model>'. The hold's rule depends
-- on it, and a verdict from another mode than the one now set is rescanned.
-- Null is a row from before the column, which was always static.

ALTER TABLE public.skill_scans ADD COLUMN IF NOT EXISTS scan_mode TEXT;
