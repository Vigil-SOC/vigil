-- When the supervisory model's scheduled check last ran and when the next is due,
-- for the page's status. One row, rewritten by each check; a heartbeat, so it is
-- not a setting and not on the config audit trail.

CREATE TABLE IF NOT EXISTS public.supervisory_worker_state (
    id SMALLINT PRIMARY KEY
        CONSTRAINT supervisory_worker_state_one_row CHECK (id = 1),
    last_check_at TIMESTAMP WITH TIME ZONE NOT NULL,
    next_check_at TIMESTAMP WITH TIME ZONE NOT NULL,
    -- The checks that raised on the last run, comma-separated; empty when none did.
    last_error TEXT NOT NULL DEFAULT ''
);

GRANT SELECT, INSERT, UPDATE ON TABLE public.supervisory_worker_state TO vigil_app;
