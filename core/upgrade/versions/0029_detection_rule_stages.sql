-- Where each overlay rule stands in its rollout (#3723). Append-only: the
-- current stage of a rule is its latest row, never a column. sync.py writes
-- 'shadow' for a new rule or version and 'off' for a removed one, for the
-- splunk and elastic copies in Vigil's overlay source only.
CREATE TABLE IF NOT EXISTS public.detection_rule_stages (
    id bigserial NOT NULL,
    rule_id integer NOT NULL,
    version integer NOT NULL,
    stage text NOT NULL,
    -- What earned the move; empty for the sync's own rows.
    proof jsonb DEFAULT '{}'::jsonb NOT NULL,
    actor text NOT NULL,
    reason text DEFAULT '' NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT detection_rule_stages_pkey PRIMARY KEY (id),
    CONSTRAINT detection_rule_stages_rule_id_fkey FOREIGN KEY (rule_id)
        REFERENCES public.detection_rules (id),
    CONSTRAINT detection_rule_stages_stage_check
        CHECK (stage IN ('shadow', 'live', 'off')),
    CONSTRAINT detection_rule_stages_version_check CHECK (version >= 1)
);

CREATE INDEX IF NOT EXISTS idx_detection_rule_stages_rule
    ON public.detection_rule_stages (rule_id, id);

-- Append-only, as detection_rule_versions: the app reads and adds, never rewrites.
REVOKE ALL ON TABLE public.detection_rule_stages FROM vigil_app;
GRANT SELECT, INSERT ON TABLE public.detection_rule_stages TO vigil_app;

COMMENT ON TABLE public.detection_rule_stages IS
    'Append-only: a row each time an overlay rule changed stage; the latest is current.';
