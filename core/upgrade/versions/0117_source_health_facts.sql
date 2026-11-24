-- Facts a collector reads from the source itself (#5071): OpenCTI's version,
-- newest intelligence and connector backlog. Null until the first read.
ALTER TABLE public.source_health ADD COLUMN facts jsonb;
