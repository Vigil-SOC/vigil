-- The Case is the only subject of shared_iocs (#4815, epic #4580). The upgrade
-- runs this only once every Case has been reconciled (plan() refuses otherwise),
-- so the legacy Investigation rows count for nothing and go.

DELETE FROM public.shared_iocs WHERE case_id IS NULL;

-- Takes idx_shared_ioc_investigation and shared_iocs_investigation_id_fkey with it.
ALTER TABLE public.shared_iocs DROP COLUMN IF EXISTS investigation_id;

ALTER TABLE public.shared_iocs ALTER COLUMN case_id SET NOT NULL;

-- One plain unique key on (case_id, ioc_type, value); it serves reads by Case
-- alone too, so the separate case_id index goes.
DROP INDEX IF EXISTS public.idx_shared_ioc_case;
DROP INDEX IF EXISTS public.uq_shared_ioc_case_key;
CREATE UNIQUE INDEX uq_shared_ioc_case_key
    ON public.shared_iocs (case_id, ioc_type, value);

DROP INDEX IF EXISTS public.idx_case_indicators_unsynced;
ALTER TABLE public.cases ALTER COLUMN indicators_synced_at SET NOT NULL;
