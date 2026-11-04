-- A SIEM copy's origin (#3706): the repo rule it came from, and how that was
-- decided. A copy has one origin; there is no links table.
--   same_id  the inventory sync linked it: same native key, same language;
--   agent    an agent or person linked it;
--   declined an agent or person said this copy is not from origin_rule_id.
-- The sync only ever writes rows with no link_kind or 'same_id'.
ALTER TABLE public.detection_rules
    ADD COLUMN IF NOT EXISTS origin_rule_id integer,
    ADD COLUMN IF NOT EXISTS link_kind text,
    ADD COLUMN IF NOT EXISTS linked_by text,
    ADD COLUMN IF NOT EXISTS link_reason text,
    ADD COLUMN IF NOT EXISTS linked_at timestamp with time zone;

ALTER TABLE public.detection_rules DROP CONSTRAINT IF EXISTS detection_rules_origin_rule_id_fkey;
ALTER TABLE public.detection_rules ADD CONSTRAINT detection_rules_origin_rule_id_fkey
    FOREIGN KEY (origin_rule_id) REFERENCES public.detection_rules (id);

ALTER TABLE public.detection_rules DROP CONSTRAINT IF EXISTS detection_rules_link_kind_check;
ALTER TABLE public.detection_rules ADD CONSTRAINT detection_rules_link_kind_check
    CHECK ((link_kind IN ('same_id', 'agent', 'declined') AND origin_rule_id IS NOT NULL)
           OR (link_kind IS NULL AND origin_rule_id IS NULL));

CREATE INDEX IF NOT EXISTS idx_detection_rules_origin_rule_id
    ON public.detection_rules (origin_rule_id);
