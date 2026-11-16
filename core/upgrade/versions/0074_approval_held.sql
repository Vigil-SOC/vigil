-- A stop holds an approved outside action until a person releases it (#4431).
-- Default false: rows the executor was already going to run stay runnable
-- unless the ceiling is stopped at upgrade, in which case those approved
-- rows are marked held. isolate_host and the Cloudflare action types are
-- the ones execute_approved_actions runs.

ALTER TABLE public.approval_actions
    ADD COLUMN held boolean NOT NULL DEFAULT false;

UPDATE public.approval_actions AS a
SET held = true
WHERE a.status = 'approved'
  AND a.action_type IN ('isolate_host', 'waf_block', 'gateway_block', 'access_revoke')
  AND EXISTS (
    SELECT 1
    FROM public.system_config AS c
    WHERE c.key = 'autonomy.ceiling'
      AND c.value->>'stopped' = 'true'
  );
