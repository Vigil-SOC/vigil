-- The feed's cursor (#2767, decision 7). One row, outside the chain: the
-- sender advances it only after a batch is on the socket. vigil_app updates
-- the row and cannot insert or delete it.

CREATE TABLE public.audit_feed_state (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_delivered_seq bigint,
    last_delivered_at timestamp with time zone,
    last_error text,
    failing boolean DEFAULT false NOT NULL,
    -- The 15-minute checkpoint waits from here, and covers entries through
    -- checkpoint_through (the tip after that checkpoint was written).
    last_checkpoint_at timestamp with time zone DEFAULT now() NOT NULL,
    checkpoint_through bigint DEFAULT 0 NOT NULL,
    -- Byte offset into $VIGIL_UPGRADE_DIR/history.jsonl already imported.
    history_offset bigint DEFAULT 0 NOT NULL
);

INSERT INTO public.audit_feed_state (id) VALUES (1);

COMMENT ON TABLE public.audit_feed_state IS
    'Syslog feed cursor and leader bookkeeping. Outside the audit chain. One row.';

REVOKE ALL ON TABLE public.audit_feed_state FROM vigil_app;
GRANT SELECT, UPDATE ON TABLE public.audit_feed_state TO vigil_app;
