-- Sampled agent dismissals and whether the person opened the evidence (#4382).
-- No backfill: existing closes were not drawn, and the flag was not recorded.

ALTER TABLE public.case_closure_info
    ADD COLUMN sampled_at timestamp without time zone,
    ADD COLUMN evidence_opened boolean;
