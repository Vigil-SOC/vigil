-- One row the audit leader (core/audit/feed.py) keeps outside the chain (#2767,
-- decision 10): how far the syslog feed has delivered, whether it is failing,
-- when the leader last wrote a timed checkpoint, and how far it has read
-- $VIGIL_UPGRADE_DIR/history.jsonl. The trail itself is the feed's backlog.

CREATE TABLE public.audit_feed_state (
    id smallint DEFAULT 1 PRIMARY KEY CHECK (id = 1),
    delivered_seq bigint DEFAULT 0 NOT NULL,
    delivered_at timestamp with time zone,
    failing boolean DEFAULT false NOT NULL,
    last_error text,
    last_error_at timestamp with time zone,
    checkpoint_seq bigint DEFAULT 0 NOT NULL,
    checkpoint_at timestamp with time zone,
    upgrade_offset bigint DEFAULT 0 NOT NULL
);
INSERT INTO public.audit_feed_state (id) VALUES (1);

COMMENT ON TABLE public.audit_feed_state IS
    'The audit leader''s one row: feed cursor and health, timed checkpoint, upgrade-history offset. Not part of the chain.';

REVOKE ALL ON TABLE public.audit_feed_state FROM vigil_app;
GRANT SELECT, UPDATE ON TABLE public.audit_feed_state TO vigil_app;
