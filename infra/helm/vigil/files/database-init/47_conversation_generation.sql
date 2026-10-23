-- A deleted conversation keeps its row. generation 0 is the ledger run that
-- already exists; each delete increments it. deleted_at hides the row until
-- a later turn continues it.
--
-- conversations is created by create_all, so a 0.8 install has the table and
-- not these columns. Adoption runs this file. Revision 0007_conversation_generation
-- is the same change for a database already stamped at 0001_baseline.

ALTER TABLE public.conversations
    ADD COLUMN IF NOT EXISTS generation integer DEFAULT 0 NOT NULL,
    ADD COLUMN IF NOT EXISTS deleted_at timestamp without time zone;

COMMENT ON COLUMN public.conversations.generation IS
    'Ledger-run generation. 0 uses run_id_for of the session id. Each delete increments it.';

COMMENT ON COLUMN public.conversations.deleted_at IS
    'Set while the conversation is deleted. Null when the row is listed.';
