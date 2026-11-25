-- A Case opened from a re-check, and the append-only calls on its queue (#5095).
-- recheck_mark is the one mark: source, close date, and the run. Null is every
-- other Case. Agreement leaves a marked Case out. Reviews are insert-only;
-- the first row is the call that counts, and a later one does not replace it.

ALTER TABLE public.cases
    ADD COLUMN IF NOT EXISTS recheck_mark JSONB;

COMMENT ON COLUMN public.cases.recheck_mark IS
    'Opened from a re-check: source, closed_at, run_id. Null otherwise.';

CREATE UNIQUE INDEX IF NOT EXISTS uq_cases_recheck_run
    ON public.cases ((recheck_mark ->> 'run_id'))
    WHERE recheck_mark IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.recheck_reviews (
    id BIGSERIAL PRIMARY KEY,
    ticket_id BIGINT NOT NULL
        REFERENCES public.capped_recheck_tickets (id) ON DELETE CASCADE,
    call TEXT NOT NULL
        CONSTRAINT recheck_reviews_call_check
        CHECK (call IN ('real', 'not real', 'can''t tell')),
    actor TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS recheck_reviews_ticket_idx
    ON public.recheck_reviews (ticket_id, id);

GRANT SELECT, INSERT, DELETE ON TABLE public.recheck_reviews TO vigil_app;
GRANT USAGE, SELECT ON SEQUENCE public.recheck_reviews_id_seq TO vigil_app;
