-- The audit webhook's cursor (#4019). One row, outside the chain, apart from
-- audit_feed_state so the webhook and syslog advance and fail independently.
-- pending_through holds a failed batch's last seq, so the resend is the same
-- batch under the same webhook-id. vigil_app updates the row and cannot
-- insert or delete it.

CREATE TABLE public.audit_webhook_state (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_delivered_seq bigint,
    last_delivered_at timestamp with time zone,
    last_error text,
    failing boolean DEFAULT false NOT NULL,
    pending_through bigint
);

INSERT INTO public.audit_webhook_state (id) VALUES (1);

COMMENT ON TABLE public.audit_webhook_state IS
    'Audit webhook cursor. Outside the audit chain. One row.';

REVOKE ALL ON TABLE public.audit_webhook_state FROM vigil_app;
GRANT SELECT, UPDATE ON TABLE public.audit_webhook_state TO vigil_app;
