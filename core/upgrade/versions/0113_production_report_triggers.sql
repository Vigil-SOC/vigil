-- Production Start Report (#4997). The same rows, two more trigger values.
-- No new column: the start used and the window live in evidence.
ALTER TABLE public.report_runs DROP CONSTRAINT report_runs_trigger_check;
ALTER TABLE public.report_runs
    ADD CONSTRAINT report_runs_trigger_check
    CHECK (trigger IN (
        'readiness', 'fallback', 'on_demand', 'production', 'production_on_demand'
    ));

ALTER TABLE public.report_records DROP CONSTRAINT report_records_trigger_check;
ALTER TABLE public.report_records
    ADD CONSTRAINT report_records_trigger_check
    CHECK (trigger IN (
        'readiness', 'fallback', 'on_demand', 'production', 'production_on_demand'
    ));
