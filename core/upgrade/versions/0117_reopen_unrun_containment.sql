-- Approved containment and Cloudflare rows nothing ran (#5059). The daemon
-- executor is gone and a containment now runs on approval, through a tool the
-- approval names; these name none, so they go back to pending for a person to
-- approve again. Who approved them, and why they were reopened, stay on the row.
UPDATE public.approval_actions
SET status = 'pending',
    parameters = COALESCE(parameters, '{}'::jsonb) || jsonb_build_object(
        'reopened', jsonb_build_object(
            'reason', 'Approved, but nothing ran it: the daemon executor that was meant to is gone. Decide it again.',
            'approved_by', approved_by,
            'approved_at', approved_at
        )
    ),
    approved_by = NULL,
    approved_by_principal_id = NULL,
    approved_at = NULL,
    held = false
WHERE status = 'approved'
  AND action_type IN (
      'isolate_host', 'disable_user', 'waf_block', 'gateway_block', 'access_revoke'
  )
  AND COALESCE(parameters ->> 'tool', '') = '';
