-- What Vigil checked about a rule version (#3704). checks['runs'] is
-- {result: pass|fail|unknown, reason, checked_at}, read from the SIEM that
-- runs the rule. A new version starts with none.
ALTER TABLE public.detection_rule_versions
    ADD COLUMN IF NOT EXISTS checks jsonb DEFAULT '{}'::jsonb NOT NULL;

-- Still append-only otherwise: checks is the one column the app may rewrite.
GRANT UPDATE (checks) ON TABLE public.detection_rule_versions TO vigil_app;

-- A SIEM rule that is listed but switched off.
ALTER TABLE public.detection_rules DROP CONSTRAINT IF EXISTS detection_rules_status_check;
ALTER TABLE public.detection_rules ADD CONSTRAINT detection_rules_status_check
    CHECK (status IN ('active', 'disabled', 'removed'));
