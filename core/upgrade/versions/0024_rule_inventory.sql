-- The rule inventory: every detection rule Vigil can read, from every source,
-- under one stable id. core/detections/inventory writes both tables.
--
-- A rule is one (source, native_key): the id the source itself gives the rule
-- (a Sigma or ESCU id, an Elastic rule_id, a Splunk app/name, a Wazuh rule
-- number, or repo path for a file with none). A rule is never deleted. One the
-- source stops listing is status 'removed', and comes back 'active' or
-- 'disabled' if the source lists it again.
--
-- rule_versions is append-only: a version is written when the rule's
-- fingerprint (a hash of its normalised logic) differs from the latest one.

CREATE SEQUENCE IF NOT EXISTS public.rules_number_seq;

CREATE TABLE IF NOT EXISTS public.rules (
    id text DEFAULT ('R-' || nextval('public.rules_number_seq')::text) NOT NULL,
    name text NOT NULL,
    source text NOT NULL,
    native_key text NOT NULL,
    techniques text[] DEFAULT '{}'::text[] NOT NULL,
    latest_version integer NOT NULL,
    status text NOT NULL,
    first_seen timestamp with time zone DEFAULT now() NOT NULL,
    last_changed timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT rules_pkey PRIMARY KEY (id),
    CONSTRAINT rules_source_native_key UNIQUE (source, native_key),
    CONSTRAINT rules_status_check CHECK (status IN ('active', 'disabled', 'removed')),
    CONSTRAINT rules_latest_version_check CHECK (latest_version >= 1)
);

ALTER SEQUENCE public.rules_number_seq OWNED BY public.rules.id;

CREATE TABLE IF NOT EXISTS public.rule_versions (
    rule_id text NOT NULL,
    version integer NOT NULL,
    fingerprint text NOT NULL,
    content jsonb NOT NULL,
    -- Where this version was read: the source and, for a repo, the commit.
    seen_in text NOT NULL,
    seen_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT rule_versions_pkey PRIMARY KEY (rule_id, version),
    CONSTRAINT rule_versions_rule_id_fkey FOREIGN KEY (rule_id)
        REFERENCES public.rules (id),
    CONSTRAINT rule_versions_version_check CHECK (version >= 1)
);

-- The app appends versions and never rewrites one.
REVOKE UPDATE, DELETE ON TABLE public.rule_versions FROM vigil_app;

COMMENT ON TABLE public.rules IS
    'Every detection rule from every source, under a stable id (R-n).';
COMMENT ON TABLE public.rule_versions IS
    'Append-only history of a rule: a row each time its logic changed.';
