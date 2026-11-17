-- Finding ids an agent's exception cited (#4397). Null when a person added it,
-- or the copy is Sigma or handed off (those are not checked). An empty array
-- is an agent exception on a copy that had no judged-false alerts. How many a
-- person still judges false is counted from these ids at read time.

ALTER TABLE public.detection_rule_exceptions
    ADD COLUMN cited_finding_ids jsonb;

ALTER TABLE public.detection_rule_exceptions
    ADD CONSTRAINT detection_rule_exceptions_cited_check
    CHECK (
        cited_finding_ids IS NULL
        OR jsonb_typeof(cited_finding_ids) = 'array'
    );
