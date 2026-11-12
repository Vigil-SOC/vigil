-- One row for the Medic status watch (#4340): the word last told to
-- holders of medic.admin, the word the next notice is confirming and how
-- many polls it has, which medic_last_seen poll those counts came from,
-- and when the current red word was first read. The watch in
-- core/platform/medic_last_seen.py is the only writer. A later change
-- adds its own columns; this revision does not.

CREATE TABLE IF NOT EXISTS public.medic_watch_state (
    id SMALLINT PRIMARY KEY DEFAULT 1
        CONSTRAINT medic_watch_state_one_row CHECK (id = 1),
    announced_word TEXT,
    confirming_word TEXT,
    confirming_polls INTEGER NOT NULL DEFAULT 0,
    counted_updated_at TIMESTAMP,
    red_since TIMESTAMP,
    CONSTRAINT medic_watch_state_announced CHECK (
        announced_word IS NULL OR announced_word IN (
            'down', 'blind', 'not_recording', 'crash_looping',
            'running', 'degraded'
        )
    ),
    CONSTRAINT medic_watch_state_confirming CHECK (
        confirming_word IS NULL OR confirming_word IN (
            'down', 'blind', 'not_recording', 'crash_looping',
            'running', 'degraded'
        )
    ),
    CONSTRAINT medic_watch_state_polls CHECK (confirming_polls >= 0)
);

INSERT INTO public.medic_watch_state (id) VALUES (1)
    ON CONFLICT (id) DO NOTHING;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.medic_watch_state TO vigil_app;
