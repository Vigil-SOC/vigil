-- A person's proposed detection on its way from a missed attack step to a rule in
-- the SIEM (#4157, ported from 1.x's 0037). The moves between states are
-- TRANSITIONS in core/storage/models/detection_proposal.py; every step tried,
-- failed ones included, appends a detection_proposal_events row.
CREATE TABLE IF NOT EXISTS public.detection_proposals (
    proposal_id character varying(36) NOT NULL,
    title text NOT NULL,
    technique text,
    case_id character varying(50),
    -- What it was drafted from (a person's paste, an agent's save, a Case step).
    origin jsonb DEFAULT '{}'::jsonb NOT NULL,
    sigma text NOT NULL,
    -- The native copies Convert attached: {"splunk": yaml, "elastic": toml, "wazuh": xml}.
    copies jsonb DEFAULT '{}'::jsonb NOT NULL,
    state character varying(16) NOT NULL,
    -- The overlay rule, once Shadow saved one. Its rollout is detection_rule_stages.
    rule_id integer,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT detection_proposals_pkey PRIMARY KEY (proposal_id),
    CONSTRAINT detection_proposals_case_id_fkey FOREIGN KEY (case_id)
        REFERENCES public.cases (case_id) ON DELETE SET NULL,
    CONSTRAINT detection_proposals_rule_id_fkey FOREIGN KEY (rule_id)
        REFERENCES public.detection_rules (id),
    CONSTRAINT detection_proposals_state_check CHECK (state IN (
        'draft', 'linted', 'replayed', 'converted', 'shadow', 'live', 'rejected'
    )),
    CONSTRAINT detection_proposals_copies_check CHECK (jsonb_typeof(copies) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_detection_proposals_state
    ON public.detection_proposals USING btree (state);
CREATE INDEX IF NOT EXISTS idx_detection_proposals_case_id
    ON public.detection_proposals USING btree (case_id);
CREATE INDEX IF NOT EXISTS idx_detection_proposals_rule_id
    ON public.detection_proposals USING btree (rule_id);

CREATE TABLE IF NOT EXISTS public.detection_proposal_events (
    id bigserial NOT NULL,
    proposal_id character varying(36) NOT NULL,
    -- Null only on the row that created the proposal.
    from_state character varying(16),
    to_state character varying(16) NOT NULL,
    -- The rule as it stood, so a lint or replay result names what it judged.
    sigma text NOT NULL,
    evidence jsonb NOT NULL,
    actor text NOT NULL,
    -- A signed-in person, an agent run, or Vigil itself.
    actor_kind character varying(8) NOT NULL,
    reason text DEFAULT '' NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT detection_proposal_events_pkey PRIMARY KEY (id),
    CONSTRAINT detection_proposal_events_proposal_id_fkey FOREIGN KEY (proposal_id)
        REFERENCES public.detection_proposals (proposal_id),
    CONSTRAINT detection_proposal_events_from_state_check CHECK (from_state IN (
        'draft', 'linted', 'replayed', 'converted', 'shadow', 'live', 'rejected'
    )),
    CONSTRAINT detection_proposal_events_to_state_check CHECK (to_state IN (
        'draft', 'linted', 'replayed', 'converted', 'shadow', 'live', 'rejected'
    )),
    CONSTRAINT detection_proposal_events_created_as_draft_check
        CHECK (from_state IS NOT NULL OR to_state = 'draft'),
    CONSTRAINT detection_proposal_events_evidence_check
        CHECK (jsonb_typeof(evidence) = 'object' AND evidence <> '{}'::jsonb),
    CONSTRAINT detection_proposal_events_actor_check CHECK (actor <> ''),
    CONSTRAINT detection_proposal_events_actor_kind_check
        CHECK (actor_kind IN ('person', 'agent', 'system'))
);

CREATE INDEX IF NOT EXISTS idx_detection_proposal_events_proposal
    ON public.detection_proposal_events (proposal_id, id);

-- The record is never rewritten, whoever connects: the app role holds no UPDATE
-- or DELETE, and this trigger refuses them for the owner too. A TRUNCATE (a
-- test resetting detection_rules ... CASCADE) is not a row change and passes.
CREATE OR REPLACE FUNCTION public.detection_proposal_events_append_only()
    RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'detection_proposal_events is append-only: % refused', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END
$$;

DROP TRIGGER IF EXISTS detection_proposal_events_append_only
    ON public.detection_proposal_events;
CREATE TRIGGER detection_proposal_events_append_only
    BEFORE UPDATE OR DELETE ON public.detection_proposal_events
    FOR EACH ROW EXECUTE FUNCTION public.detection_proposal_events_append_only();

-- A proposal is rejected, never deleted.
REVOKE ALL ON TABLE public.detection_proposals FROM vigil_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.detection_proposals TO vigil_app;
REVOKE ALL ON TABLE public.detection_proposal_events FROM vigil_app;
GRANT SELECT, INSERT ON TABLE public.detection_proposal_events TO vigil_app;
GRANT USAGE ON SEQUENCE public.detection_proposal_events_id_seq TO vigil_app;

COMMENT ON TABLE public.detection_proposals IS
    'A proposed detection and the state it is in; detection_proposal_events holds how it got there.';
COMMENT ON TABLE public.detection_proposal_events IS
    'Append-only: a row for each step tried on a proposal, with who, on what evidence and why.';
