-- One capped re-check of the Day 1 list (#5093). The draw is fixed at
-- insert. A later batch draws again and does not copy a result. The driver
-- updates a ticket when it claims the ticket and when a re-check cannot run.

CREATE TABLE IF NOT EXISTS public.capped_recheck_batches (
    batch_id VARCHAR(36) PRIMARY KEY,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    -- 'person' starts the first batch. The Day 1 sweep may start a later one.
    origin TEXT NOT NULL
        CONSTRAINT capped_recheck_batches_origin_check
        CHECK (origin IN ('person', 'sweep')),
    actor_id TEXT,
    record_id VARCHAR(36) NOT NULL REFERENCES public.report_records (record_id),
    -- running, interrupted, complete, or 'stopped: ceiling, k of n'.
    status TEXT NOT NULL,
    -- Closures in the section windows at or after the install clock. Not drawn.
    post_install_closures INTEGER NOT NULL,
    -- How many control tickets were drawn. Fewer than 25 is that short draw.
    control_count INTEGER NOT NULL,
    resumed_at TIMESTAMP WITH TIME ZONE,
    -- The gateway remainder held the next start. The batch stays running.
    quota_hold BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS public.capped_recheck_tickets (
    id BIGSERIAL PRIMARY KEY,
    batch_id VARCHAR(36) NOT NULL
        REFERENCES public.capped_recheck_batches (batch_id) ON DELETE CASCADE,
    import_id TEXT NOT NULL,
    source TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    ticket_group TEXT NOT NULL
        CONSTRAINT capped_recheck_tickets_group_check
        CHECK (ticket_group IN ('top', 'control')),
    -- Claim order: one shuffle at draw, kept.
    position INTEGER NOT NULL,
    random_key TEXT NOT NULL,
    import_finished_at TIMESTAMP WITH TIME ZONE,
    run_id VARCHAR(80),
    -- Set while this process is calling the starter, so a second replica waits.
    -- dispatch_token changes when another replica takes over a cut-off claim.
    dispatch_until TIMESTAMP WITH TIME ZONE,
    dispatch_token TEXT,
    -- Null until the ticket is settled. A reason stays in the denominator.
    reason TEXT,
    CONSTRAINT capped_recheck_tickets_batch_position_key UNIQUE (batch_id, position)
);

CREATE INDEX IF NOT EXISTS capped_recheck_tickets_batch_idx
    ON public.capped_recheck_tickets (batch_id, position);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.capped_recheck_batches TO vigil_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.capped_recheck_tickets TO vigil_app;
GRANT USAGE, SELECT ON SEQUENCE public.capped_recheck_tickets_id_seq TO vigil_app;
