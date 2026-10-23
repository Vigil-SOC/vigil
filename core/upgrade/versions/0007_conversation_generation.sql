-- Deleting a conversation keeps the row (#2856). generation 0 is the ledger
-- run already written for this id. A later life uses a new run. deleted_at
-- hides the row from history until a later turn clears it.

ALTER TABLE public.conversations
    ADD COLUMN IF NOT EXISTS generation integer DEFAULT 0 NOT NULL,
    ADD COLUMN IF NOT EXISTS deleted_at timestamp without time zone;

COMMENT ON COLUMN public.conversations.generation IS
    'Ledger life of this conversation. 0 is run_id_for(id); N is run_id_for(id:N).';

COMMENT ON COLUMN public.conversations.deleted_at IS
    'Set when the conversation is deleted. History hides the row until a later turn clears it.';
