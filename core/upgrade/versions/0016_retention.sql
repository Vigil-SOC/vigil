-- Announcement sequences that have not yet left the box (#2769). A push, a
-- poll, or an export removes one when it actually carries that row. The trail
-- row itself is never updated. vigil_app already updates this one-row table.

ALTER TABLE public.audit_feed_state
    ADD COLUMN pending_announcements bigint[] DEFAULT '{}'::bigint[] NOT NULL;

COMMENT ON COLUMN public.audit_feed_state.pending_announcements IS
    'Trail seqs of retention and erasure announcements not yet carried out by a push, a poll, or an export. Outside the chain.';
