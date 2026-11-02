-- One analyst's statement that an entity doing something is expected, why, and
-- until when. It explains alerts to people and agents. Nothing that moves an
-- alert reads it, and there is no effect column: the row can only inform.
--
-- entity_kind and entity_value are the two halves of an Entity Key, minted by
-- core.memory.entity_keys, so a rule joins on what a finding's entities mint to.
-- An entity may carry several rules, one per thing it is expected to do. Removal
-- is recorded and never deleted. vigil_app gets its access from the schema's
-- default privileges, as other tables do.

CREATE TABLE public.known_activity (
    known_activity_id character varying(50) PRIMARY KEY,
    entity_kind character varying(20) NOT NULL,
    entity_value text NOT NULL,
    reason text NOT NULL,
    expires_at timestamp without time zone NOT NULL,
    created_by character varying(100) NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    removed_at timestamp without time zone,
    removed_by character varying(100),
    removal_reason text,
    origin character varying(20) DEFAULT 'ad_hoc'::character varying NOT NULL,
    origin_ref character varying(100),
    CONSTRAINT ck_known_activity_kind CHECK (entity_kind IN ('ip', 'domain', 'host', 'url', 'email', 'hash', 'arn', 'aws_key', 'user', 'process')),
    CONSTRAINT ck_known_activity_origin CHECK (origin IN ('ad_hoc', 'finding', 'case', 'run')),
    CONSTRAINT ck_known_activity_removal CHECK ((removed_at IS NULL) = (removed_by IS NULL)),
    CONSTRAINT ck_known_activity_expiry CHECK (expires_at > created_at)
);

CREATE INDEX idx_known_activity_entity ON public.known_activity (entity_kind, entity_value) WHERE removed_at IS NULL;
CREATE INDEX idx_known_activity_created_at ON public.known_activity (created_at DESC);

COMMENT ON TABLE public.known_activity IS
    'Analyst context about an entity: expected, why, until when. Read to explain alerts; the queue, triage, severity, verdicts, intake, exclusions and case state never consult it.';
