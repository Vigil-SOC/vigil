-- Permission catalog (#2915). Copy successors onto every role, then the reads
-- a signed-in user can already complete. settings.read and settings.write stay:
-- today's gates still check them, and #2916 deletes the keys when it moves
-- those gates. A false successor is not a check (Analyst's cases.assign), so
-- the copy overwrites it. A missing parent adds nothing. Keys this statement
-- does not name stay, and an existing value of a key it does not name wins.

UPDATE public.roles
SET permissions = permissions
    || CASE WHEN permissions->>'settings.write' = 'true' THEN
         '{"detections.write": true, "automation.write": true, "ai_autonomy.write": true, "ai_autonomy.stop": true, "platform.write": true}'::jsonb
       ELSE '{}'::jsonb END
    || CASE WHEN permissions->>'settings.read' = 'true' THEN
         '{"detections.read": true, "automation.read": true, "ai_autonomy.read": true, "platform.read": true}'::jsonb
       ELSE '{}'::jsonb END
    || CASE WHEN permissions->>'integrations.write' = 'true' THEN
         '{"integrations.install": true}'::jsonb
       ELSE '{}'::jsonb END
    || CASE WHEN permissions->>'cases.write' = 'true' THEN
         '{"cases.assign": true}'::jsonb
       ELSE '{}'::jsonb END
    -- Unchecked reads, including where the parent key is false or missing.
    || '{"detections.read": true, "automation.read": true, "ai_autonomy.read": true, "platform.read": true, "ai_decisions.read": true, "integrations.read": true}'::jsonb
    -- Viewer's seeded false. A role with no settings.read key is left without one.
    || CASE WHEN permissions ? 'settings.read' THEN '{"settings.read": true}'::jsonb
       ELSE '{}'::jsonb END
    -- Widening: Senior Analyst does not hold settings.write, so stop is not a copy.
    || CASE WHEN role_id = 'role-senior-analyst' THEN '{"ai_autonomy.stop": true}'::jsonb
       ELSE '{}'::jsonb END
    -- Built-in Admin only. A kept legacy admin row is not role-admin.
    || CASE WHEN role_id = 'role-admin' THEN '{"roles.manage": true}'::jsonb
       ELSE '{}'::jsonb END,
    updated_at = NOW();

-- Confidence marked these approved with nobody in approved_by. They go back
-- to pending; a person still has to decide them.
UPDATE public.approval_actions
SET status = 'pending'
WHERE status = 'approved'
  AND requires_approval = false
  AND approved_by IS NULL;
