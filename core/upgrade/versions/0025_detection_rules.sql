-- Every detection rule Vigil reads (#3703). core/detections/inventory/sync.py
-- writes both tables; the agent detection tools read them.
--
-- A row is one copy of a rule: (source, native_key). source is
-- 'repo:<detection source id>'; native_key is the file's own id (Sigma/ESCU
-- id, Elastic rule_id), or its path in the source when it has none. A rule
-- is never deleted: one its source stops listing is status 'removed'.

CREATE TABLE IF NOT EXISTS public.detection_rules (
    id serial NOT NULL,
    source text NOT NULL,
    native_key text NOT NULL,
    name text NOT NULL,
    -- The rule format: sigma, splunk, elastic or kql.
    language text NOT NULL,
    techniques text[] DEFAULT '{}'::text[] NOT NULL,
    tactics text[] DEFAULT '{}'::text[] NOT NULL,
    status text DEFAULT 'active' NOT NULL,
    latest_version integer NOT NULL,
    -- Where the copy was last read, relative to the source root.
    path text,
    CONSTRAINT detection_rules_pkey PRIMARY KEY (id),
    CONSTRAINT detection_rules_source_native_key UNIQUE (source, native_key),
    CONSTRAINT detection_rules_status_check CHECK (status IN ('active', 'removed')),
    CONSTRAINT detection_rules_latest_version_check CHECK (latest_version >= 1)
);

CREATE INDEX IF NOT EXISTS idx_detection_rules_techniques
    ON public.detection_rules USING gin (techniques);

-- A version is written only when the rule's fingerprint (a hash of what
-- changes what it fires on) differs from the latest one.
CREATE TABLE IF NOT EXISTS public.detection_rule_versions (
    rule_id integer NOT NULL,
    version integer NOT NULL,
    fingerprint text NOT NULL,
    -- The rule as parsed; search_detections matches over it.
    content jsonb NOT NULL,
    query text DEFAULT '' NOT NULL,
    first_seen timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT detection_rule_versions_pkey PRIMARY KEY (rule_id, version),
    CONSTRAINT detection_rule_versions_rule_id_fkey FOREIGN KEY (rule_id)
        REFERENCES public.detection_rules (id),
    CONSTRAINT detection_rule_versions_version_check CHECK (version >= 1)
);

-- Append-only: the app reads and adds versions, never rewrites one (agent_events).
REVOKE ALL ON TABLE public.detection_rule_versions FROM vigil_app;
GRANT SELECT, INSERT ON TABLE public.detection_rule_versions TO vigil_app;

COMMENT ON TABLE public.detection_rules IS
    'Every detection rule read from a configured source, one row per copy.';
COMMENT ON TABLE public.detection_rule_versions IS
    'Append-only: a row each time a rule''s logic changed.';
