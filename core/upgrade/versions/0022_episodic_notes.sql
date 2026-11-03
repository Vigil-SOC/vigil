-- What a person on the team said about an entity (#3605). Recall returns it as
-- the fourth kind, `notes`, under investigation_kind 'analyst'. There is no
-- update path: a note is written or withdrawn, and withdrawing deletes the row.
-- entity_key is normalised by core/memory/entity_keys.py; its type is one of
-- ENTITY_KEY_TYPES in core/memory/recall_contract.py.

CREATE TABLE IF NOT EXISTS public.episodic_notes (
    id bigserial NOT NULL,
    entity_key text NOT NULL,
    statement text NOT NULL,
    written_by text NOT NULL,
    -- When it was written, named for the recall order every kind shares.
    concluded_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT episodic_notes_pkey PRIMARY KEY (id),
    CONSTRAINT episodic_notes_statement_check CHECK (length(statement) > 0)
);

-- The recall join and the order it reads in, as idx_episodic_sightings_recall.
CREATE INDEX IF NOT EXISTS idx_episodic_notes_recall
    ON public.episodic_notes USING btree (entity_key, concluded_at DESC, id);

COMMENT ON TABLE public.episodic_notes IS
    'What an analyst said about an entity: context recall carries to every run that names it.';
