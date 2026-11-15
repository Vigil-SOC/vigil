-- A false-positive close and a noise mark can each store one cause (#4396).
-- No backfill: absence is null. No check constraint: the set lives in code.

ALTER TABLE public.case_closure_info
    ADD COLUMN false_positive_cause text;

ALTER TABLE public.findings
    ADD COLUMN noise_cause text;
