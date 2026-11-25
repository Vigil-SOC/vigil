-- The supervisory model's suggestions to change a setting, and what people did
-- with them. A suggestion keeps its card (why, evidence, replay) as it was made.
-- History is one row per apply, undo or dismissal. Only the follow-up that reads
-- whether results improved writes back to a row: improved and note.

CREATE TABLE IF NOT EXISTS public.supervisory_suggestions (
    suggestion_id VARCHAR(36) PRIMARY KEY,
    fn TEXT NOT NULL
        CONSTRAINT supervisory_suggestions_fn_check
        CHECK (fn IN ('triage', 'investigation', 'response')),
    setting_key TEXT NOT NULL,
    verb TEXT NOT NULL,
    from_value TEXT NOT NULL,
    to_value TEXT NOT NULL,
    widens_autonomy BOOLEAN NOT NULL DEFAULT false,
    -- why, evidence, replay, check, plain_words, table, replay_title, replay_rows
    card JSONB NOT NULL,
    -- open: shown. held: waits on a Medic incident. superseded: a newer one for
    -- the same setting replaced it.
    state TEXT NOT NULL
        CONSTRAINT supervisory_suggestions_state_check
        CHECK (state IN ('open', 'held', 'dismissed', 'applied', 'undone', 'superseded')),
    -- {id, title, firing_since} while held. Null otherwise.
    held_incident JSONB,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT supervisory_suggestions_held_check
        CHECK (state <> 'held' OR held_incident IS NOT NULL)
);

-- One live suggestion per setting.
CREATE UNIQUE INDEX IF NOT EXISTS uq_supervisory_suggestions_live
    ON public.supervisory_suggestions (setting_key)
    WHERE state IN ('open', 'held');

CREATE TABLE IF NOT EXISTS public.supervisory_history (
    id BIGSERIAL PRIMARY KEY,
    suggestion_id VARCHAR(36) NOT NULL
        REFERENCES public.supervisory_suggestions (suggestion_id) ON DELETE RESTRICT,
    at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    setting_key TEXT NOT NULL,
    from_value TEXT NOT NULL,
    to_value TEXT NOT NULL,
    outcome TEXT NOT NULL
        CONSTRAINT supervisory_history_outcome_check
        CHECK (outcome IN ('applied', 'dismissed', 'undone')),
    -- dismissed: the reason given. applied: what changed since.
    note TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    actor_principal_id TEXT,
    -- applied only: whether results improved after. Null until the follow-up reads it.
    improved BOOLEAN
);

CREATE INDEX IF NOT EXISTS supervisory_history_at_idx
    ON public.supervisory_history (at DESC, id DESC);

GRANT SELECT, INSERT, UPDATE ON TABLE public.supervisory_suggestions TO vigil_app;
GRANT SELECT, INSERT ON TABLE public.supervisory_history TO vigil_app;
GRANT UPDATE (improved, note) ON TABLE public.supervisory_history TO vigil_app;
GRANT USAGE, SELECT ON SEQUENCE public.supervisory_history_id_seq TO vigil_app;
