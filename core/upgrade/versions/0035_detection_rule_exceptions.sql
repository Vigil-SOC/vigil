-- A customer-specific exception to one overlay copy (#3794): documents of that
-- copy's runs whose fields equal every pair of match ({field: value}, the
-- copy's own SIEM field names; a CIDR value matches an address inside it) are
-- dropped by Vigil's runner before they are counted or become findings. The
-- rule body stays free of the customer's details; the exceptions are listed in
-- the deploy pull request's description, and are not applied once the SIEM's
-- own copy runs the rule.
--
-- Removal is recorded, not deleted (the ip_exclusions pattern): who removed it,
-- when and why stays. Adding or removing one makes no rule version.
CREATE TABLE IF NOT EXISTS public.detection_rule_exceptions (
    id bigserial NOT NULL,
    rule_id integer NOT NULL,
    match jsonb NOT NULL,
    note text NOT NULL,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    removed_at timestamp with time zone,
    removed_by text,
    removal_reason text,
    CONSTRAINT detection_rule_exceptions_pkey PRIMARY KEY (id),
    CONSTRAINT detection_rule_exceptions_rule_id_fkey
        FOREIGN KEY (rule_id) REFERENCES public.detection_rules(id),
    CONSTRAINT detection_rule_exceptions_match_check
        CHECK (jsonb_typeof(match) = 'object' AND match <> '{}'::jsonb),
    CONSTRAINT detection_rule_exceptions_note_check CHECK (btrim(note) <> ''),
    CONSTRAINT detection_rule_exceptions_removal_check
        CHECK ((removed_at IS NULL) = (removed_by IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_detection_rule_exceptions_active
    ON public.detection_rule_exceptions USING btree (rule_id)
    WHERE removed_at IS NULL;

-- History is never deleted: the app may only fill in the removal columns.
REVOKE ALL ON TABLE public.detection_rule_exceptions FROM vigil_app;
GRANT SELECT, INSERT ON TABLE public.detection_rule_exceptions TO vigil_app;
GRANT UPDATE (removed_at, removed_by, removal_reason)
    ON TABLE public.detection_rule_exceptions TO vigil_app;
GRANT USAGE ON SEQUENCE public.detection_rule_exceptions_id_seq TO vigil_app;
