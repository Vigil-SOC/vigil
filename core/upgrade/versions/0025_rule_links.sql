-- Rule links: two rules in the inventory (0024) that are copies of one rule
-- in different places, e.g. a repo rule and the copy running in a SIEM.
-- core/detections/inventory/links.py writes them.
--
-- A pair is stored once, smaller id first (bytewise, as Python compares).
-- `how` says what matched, most certain first: the source kept the rule's own
-- id (same_id), the copy carries "vigil:R-n" (vigil_tag), the copy has the
-- repo rule's vendor name (known_name), or the logic looks the same
-- (looks_same). Only looks_same waits for a person: it starts 'pending', and a
-- person makes it 'confirmed' or 'rejected'. The rest are 'auto'.
--
-- A link is never deleted: one to a rule later marked removed is history.

CREATE TABLE IF NOT EXISTS public.rule_links (
    rule_a text NOT NULL,
    rule_b text NOT NULL,
    how text NOT NULL,
    confirmed text NOT NULL,
    -- looks_same only: how alike the two rules' logic is, 0 to 1.
    score real,
    -- What matched: the shared id, the tag, the name, the shared values.
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    found_at timestamp with time zone DEFAULT now() NOT NULL,
    confirmed_by text,
    confirmed_at timestamp with time zone,
    CONSTRAINT rule_links_pkey PRIMARY KEY (rule_a, rule_b),
    CONSTRAINT rule_links_rule_a_fkey FOREIGN KEY (rule_a)
        REFERENCES public.rules (id),
    CONSTRAINT rule_links_rule_b_fkey FOREIGN KEY (rule_b)
        REFERENCES public.rules (id),
    CONSTRAINT rule_links_order_check CHECK ((rule_a COLLATE "C") < (rule_b COLLATE "C")),
    CONSTRAINT rule_links_how_check
        CHECK (how IN ('same_id', 'vigil_tag', 'known_name', 'looks_same')),
    CONSTRAINT rule_links_confirmed_check
        CHECK (confirmed IN ('auto', 'pending', 'confirmed', 'rejected'))
);

-- The primary key serves lookups by rule_a; this one serves rule_b.
CREATE INDEX IF NOT EXISTS idx_rule_links_rule_b
    ON public.rule_links USING btree (rule_b);

-- The app adds and confirms links and never deletes one.
REVOKE DELETE ON TABLE public.rule_links FROM vigil_app;

COMMENT ON TABLE public.rule_links IS
    'Pairs of inventory rules that are copies of one rule in different places.';
