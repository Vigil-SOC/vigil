-- What a source said about an entity, read at triage and kept for its time to
-- live (#4741, epic #4719). core/integrations/entity_facts.py is the only writer;
-- the daemon's cleanup sweep deletes expired rows and rows whose Case closed.
-- facts holds only the fields the source's projection allowlists. case_id is
-- NULL for a triage read; a run on a Case writes its own row (#4742). Naive UTC
-- like the rest of the schema (core/time.py).

CREATE TABLE IF NOT EXISTS public.entity_facts (
    id BIGSERIAL PRIMARY KEY,
    entity_key VARCHAR(512) NOT NULL,
    source VARCHAR(64) NOT NULL,
    case_id VARCHAR(50) REFERENCES public.cases (case_id) ON DELETE CASCADE,
    facts JSONB NOT NULL,
    fetched_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0
);

-- One row per key, source and Case; COALESCE makes "no Case" one value.
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_facts_key_source_case
    ON public.entity_facts (entity_key, source, COALESCE(case_id, ''));

-- The sweep's expiry delete and the row cap's oldest-first eviction.
CREATE INDEX IF NOT EXISTS idx_entity_facts_expires ON public.entity_facts (expires_at);
CREATE INDEX IF NOT EXISTS idx_entity_facts_fetched ON public.entity_facts (fetched_at);
CREATE INDEX IF NOT EXISTS idx_entity_facts_case
    ON public.entity_facts (case_id) WHERE case_id IS NOT NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.entity_facts TO vigil_app;
GRANT USAGE, SELECT ON SEQUENCE public.entity_facts_id_seq TO vigil_app;
