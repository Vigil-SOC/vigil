-- known_name (#3717): the inventory sync linked a Splunk saved search named
-- 'ESCU - <name> - Rule' to the one active repo Splunk rule named <name>.
-- The sync only ever writes rows with no link_kind, 'same_id' or 'known_name'.
ALTER TABLE public.detection_rules DROP CONSTRAINT IF EXISTS detection_rules_link_kind_check;
ALTER TABLE public.detection_rules ADD CONSTRAINT detection_rules_link_kind_check
    CHECK ((link_kind IN ('same_id', 'known_name', 'agent', 'declined') AND origin_rule_id IS NOT NULL)
           OR (link_kind IS NULL AND origin_rule_id IS NULL));
