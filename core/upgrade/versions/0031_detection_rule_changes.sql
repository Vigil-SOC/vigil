-- A row each time sync records a rule's check result differently (#3729):
-- check_key 'runs' (pass|fail|unknown, from the SIEM), 'sees' (the same, from
-- whether its data arrives) or 'status' (active|disabled|removed, SIEM rules
-- only). old_result is the previous
-- row's new_result for that (rule, check), any version; NULL on first sight.
-- A flag is a (rule, check) whose latest row went pass -> fail; it is a query
-- over this table, cleared by a later row back to pass.
CREATE TABLE IF NOT EXISTS public.detection_rule_check_changes (
    id bigserial NOT NULL,
    rule_id integer NOT NULL,
    version integer NOT NULL,
    check_key text NOT NULL,
    old_result text,
    new_result text,
    reason text,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT detection_rule_check_changes_pkey PRIMARY KEY (id),
    CONSTRAINT detection_rule_check_changes_rule_id_fkey FOREIGN KEY (rule_id)
        REFERENCES public.detection_rules (id)
);

-- The latest row per (rule, check): sync's "old" result and the flag query.
CREATE INDEX IF NOT EXISTS idx_detection_rule_check_changes_latest
    ON public.detection_rule_check_changes
    USING btree (rule_id, check_key, changed_at DESC, id DESC);

-- Append-only: the app reads and adds rows, never rewrites one.
REVOKE ALL ON TABLE public.detection_rule_check_changes FROM vigil_app;
GRANT SELECT, INSERT ON TABLE public.detection_rule_check_changes TO vigil_app;
GRANT USAGE ON SEQUENCE public.detection_rule_check_changes_id_seq TO vigil_app;

COMMENT ON TABLE public.detection_rule_check_changes IS
    'Append-only: a row each time a detection rule''s check result changed.';

-- Starting rows, so an existing install's first break reads pass -> fail:
-- each rule's Runs and Sees results on its latest version, and each SIEM
-- rule's status.
INSERT INTO public.detection_rule_check_changes
    (rule_id, version, check_key, old_result, new_result, reason, changed_at)
SELECT v.rule_id, v.version, k.check_key, NULL,
       v.checks->k.check_key->>'result', v.checks->k.check_key->>'reason',
       COALESCE((v.checks->k.check_key->>'checked_at')::timestamptz, now())
FROM public.detection_rule_versions v
JOIN public.detection_rules r ON r.id = v.rule_id AND r.latest_version = v.version
CROSS JOIN (VALUES ('runs'), ('sees')) AS k (check_key)
WHERE v.checks ? k.check_key
  AND NOT EXISTS (SELECT 1 FROM public.detection_rule_check_changes);

INSERT INTO public.detection_rule_check_changes
    (rule_id, version, check_key, old_result, new_result, reason, changed_at)
SELECT r.id, r.latest_version, 'status', NULL, r.status, NULL, now()
FROM public.detection_rules r
WHERE r.source NOT LIKE 'repo:%'
  AND NOT EXISTS (
      SELECT 1 FROM public.detection_rule_check_changes c WHERE c.check_key = 'status'
  );
