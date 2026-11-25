-- The alert ids a file-imported ticket names (#5092). Connected pulls leave
-- this empty: their ticket_id is the Defender alert id or the Sentinel
-- incident name. Imports already stored stay empty; nothing backfills them.

ALTER TABLE public.history_tickets
    ADD COLUMN IF NOT EXISTS alert_ids TEXT[] NOT NULL DEFAULT '{}';
