-- The latest investigation-model replay (#4389). One row: a new start replaces it.

CREATE TABLE public.investigation_replay (
    id smallint NOT NULL,
    status character varying(16) NOT NULL,
    reason text,
    provider_id character varying(64) NOT NULL,
    model_id character varying(200) NOT NULL,
    started_by character varying(64) NOT NULL,
    started_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    total integer NOT NULL,
    replayed integer NOT NULL,
    skipped integer NOT NULL,
    false_closes integer NOT NULL,
    false_close_rate double precision,
    false_close_wilson_low double precision,
    false_close_wilson_high double precision,
    agreed integer NOT NULL,
    disagreed integer NOT NULL,
    abstained integer NOT NULL,
    agreement_rate double precision,
    agreement_wilson_low double precision,
    agreement_wilson_high double precision,
    cost_usd double precision NOT NULL,
    false_close jsonb NOT NULL,
    CONSTRAINT investigation_replay_pkey PRIMARY KEY (id),
    CONSTRAINT investigation_replay_one CHECK (id = 1),
    CONSTRAINT investigation_replay_status_check CHECK (
        status::text = ANY (ARRAY['running'::varchar, 'succeeded'::varchar, 'failed'::varchar])
    )
);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.investigation_replay TO vigil_app;
