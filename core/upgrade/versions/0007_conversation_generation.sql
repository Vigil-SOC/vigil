-- A deleted conversation keeps its row so the next life of the same id
-- uses a new ledger run. Generation 0 is the run that already exists.
-- deleted_at hides the row until a later turn continues it.

ALTER TABLE public.conversations
    ADD COLUMN IF NOT EXISTS generation integer DEFAULT 0 NOT NULL,
    ADD COLUMN IF NOT EXISTS deleted_at timestamp without time zone;

COMMENT ON COLUMN public.conversations.generation IS
    'Ledger-run generation. 0 uses run_id_for of the session id. Each delete increments it.';

COMMENT ON COLUMN public.conversations.deleted_at IS
    'Set while the conversation is deleted. Null when the row is listed.';
