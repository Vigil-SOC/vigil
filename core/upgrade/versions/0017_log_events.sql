-- Telemetry events for the Logs page: one row per event, every source in one
-- schema, with the source's own record kept beside it. Startup's create_all
-- may already have built the table from the model, so every step tolerates it.

CREATE TABLE IF NOT EXISTS public.log_events (
    event_id character varying(64) NOT NULL,
    "time" timestamp with time zone NOT NULL,
    source character varying(32) NOT NULL,
    event_class character varying(64) NOT NULL,
    severity character varying(16) NOT NULL,
    src_ip character varying(64),
    src_host character varying(255),
    dst_ip character varying(64),
    dst_host character varying(255),
    dst_port integer,
    user_name character varying(128),
    action character varying(64),
    summary text NOT NULL,
    mitre character varying(32),
    loglm double precision,
    incident character varying(32),
    tags jsonb NOT NULL,
    raw jsonb NOT NULL,
    search_text text NOT NULL,
    CONSTRAINT log_events_pkey PRIMARY KEY (event_id)
);

CREATE INDEX IF NOT EXISTS ix_log_events_time
    ON public.log_events USING btree ("time");
CREATE INDEX IF NOT EXISTS ix_log_events_source_time
    ON public.log_events USING btree (source, "time");
CREATE INDEX IF NOT EXISTS ix_log_events_incident
    ON public.log_events USING btree (incident);

COMMENT ON TABLE public.log_events IS
    'Telemetry events posted to /api/logs/events and searched by the Logs page. search_text is the lower-cased readable fields and raw record.';
