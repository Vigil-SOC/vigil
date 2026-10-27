-- Decision 11 (#3191). A restore sets unconfirmed_seq to its trail entry, and
-- pause_until to 30 seconds on when the feed is on. trail.append refuses
-- until then, or until a push, poll or export carries that entry and clears
-- both. vigil_app already updates the row.

ALTER TABLE public.audit_feed_state
    ADD COLUMN pause_until timestamp with time zone,
    ADD COLUMN unconfirmed_seq bigint;
