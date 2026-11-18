-- A Case is the subject of the shared IOC index (#4733, epic #4580). Intake joins
-- an alert to a Case through shared_iocs; a row now belongs to a Case directly
-- (case_id), not only through an Investigation. Legacy rows keep their
-- investigation_id with case_id NULL, so the previous release still runs. The IP
-- role is already in ioc_type, so there is no role column. Naive UTC like the
-- rest of the schema (core/time.py).

ALTER TABLE public.shared_iocs
    ADD COLUMN IF NOT EXISTS case_id VARCHAR(50)
        REFERENCES public.cases (case_id) ON DELETE CASCADE;

ALTER TABLE public.shared_iocs
    ALTER COLUMN investigation_id DROP NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_shared_ioc_case_key
    ON public.shared_iocs (case_id, ioc_type, value)
    WHERE case_id IS NOT NULL;

-- The cascade and matching read by Case.
CREATE INDEX IF NOT EXISTS idx_shared_ioc_case
    ON public.shared_iocs (case_id)
    WHERE case_id IS NOT NULL;

-- indicators_synced_at: when the Case's rows last matched what it holds. NULL
-- (every Case that exists now) means its legacy Investigation rows still count in
-- matching until the first reconcile. indicators_stale_since: set when a write to
-- the index failed and not yet cleared by a successful one.
ALTER TABLE public.cases
    ADD COLUMN IF NOT EXISTS indicators_synced_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS indicators_stale_since TIMESTAMP WITHOUT TIME ZONE;

-- A separate statement: a volatile default on ADD COLUMN rewrites the table.
ALTER TABLE public.cases
    ALTER COLUMN indicators_synced_at SET DEFAULT now();
