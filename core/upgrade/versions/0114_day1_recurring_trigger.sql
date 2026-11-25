-- Day 1 Report run again on a cadence (#5045). One more trigger value.
ALTER TABLE public.report_runs DROP CONSTRAINT report_runs_trigger_check;
ALTER TABLE public.report_runs
    ADD CONSTRAINT report_runs_trigger_check
    CHECK (trigger IN (
        'readiness', 'fallback', 'on_demand', 'recurring',
        'production', 'production_on_demand'
    ));

ALTER TABLE public.report_records DROP CONSTRAINT report_records_trigger_check;
ALTER TABLE public.report_records
    ADD CONSTRAINT report_records_trigger_check
    CHECK (trigger IN (
        'readiness', 'fallback', 'on_demand', 'recurring',
        'production', 'production_on_demand'
    ));
