DROP INDEX IF EXISTS public.idx_episodic_markers_origin;
ALTER TABLE public.episodic_distil_markers DROP COLUMN IF EXISTS origin_run_ids;
