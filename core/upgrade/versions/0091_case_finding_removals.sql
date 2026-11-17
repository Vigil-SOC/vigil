-- A finding a person took off a Case, so intake does not join it back (#4682).
-- core/storage/service.py writes a row for each finding an update unlinks and
-- clears it when the finding is linked again; services/daemon/orchestrator.py
-- reads it to skip that Case when matching. finding_id has no foreign key to
-- findings: the row says nothing about the finding itself and must not depend
-- on its retention. Naive UTC like the rest of the schema (core/time.py).

CREATE TABLE IF NOT EXISTS public.case_finding_removals (
    case_id VARCHAR(50) NOT NULL REFERENCES public.cases (case_id) ON DELETE CASCADE,
    finding_id VARCHAR(50) NOT NULL,
    removed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    PRIMARY KEY (case_id, finding_id)
);

CREATE INDEX IF NOT EXISTS idx_case_finding_removals_finding
    ON public.case_finding_removals (finding_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.case_finding_removals TO vigil_app;
