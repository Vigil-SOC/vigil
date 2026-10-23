-- A deleted conversation keeps its row (#2856). generation 0 is the ledger
-- run already written for this id. deleted_at hides the row until a later
-- turn clears it.
--
-- create_all builds conversations, so this file does not. Helm skips it: the
-- table is not there yet, and create_all on this release already adds the
-- columns. Adoption runs it for a 0.7 or 0.8 database, which has the table
-- and not the columns. 0007_conversation_generation.sql is the same change
-- for a database that upgrades from 0001_baseline.

DO $$
BEGIN
    IF to_regclass('public.conversations') IS NULL THEN
        RETURN;
    END IF;
    ALTER TABLE public.conversations
        ADD COLUMN IF NOT EXISTS generation integer DEFAULT 0 NOT NULL;
    ALTER TABLE public.conversations
        ADD COLUMN IF NOT EXISTS deleted_at timestamp without time zone;
    COMMENT ON COLUMN public.conversations.generation IS
        'Ledger life of this conversation. 0 is run_id_for(id); N is run_id_for(id:N).';
    COMMENT ON COLUMN public.conversations.deleted_at IS
        'Set when the conversation is deleted. History hides the row until a later turn clears it.';
END $$;
