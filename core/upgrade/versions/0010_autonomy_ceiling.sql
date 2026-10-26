-- Autonomy ceiling (#2918). The seeded approval flag is today's Act and is
-- not a person's raise, so both act flags and four-eyes start off. No
-- orchestrator.settings key is copied. Confidence-approved rows were already
-- returned to pending by 0008; this revision does not touch them again.

INSERT INTO system_config (key, value, description, config_type)
VALUES (
    'autonomy.ceiling',
    '{"act_reversible": false, "act_irreversible": false, "stopped": false, "four_eyes": false}'::jsonb,
    'Autonomy ceiling',
    'autonomy'
)
ON CONFLICT (key) DO UPDATE
SET value = EXCLUDED.value,
    description = EXCLUDED.description,
    config_type = EXCLUDED.config_type;

DELETE FROM system_config WHERE key = 'approval.force_manual_approval';

-- The catalog revision already copied these onto roles that hold the parent
-- key. Repeat the built-in grants in the form later readers expect, and only
-- where a role is still missing one. Existing keys win.
UPDATE public.roles AS r
SET permissions = g.grants || r.permissions,
    updated_at = NOW()
FROM (VALUES
    ('role-admin',
     '{"ai_autonomy.read": true, "ai_autonomy.write": true, "ai_autonomy.stop": true}'::jsonb),
    ('role-manager',
     '{"ai_autonomy.read": true, "ai_autonomy.write": true, "ai_autonomy.stop": true}'::jsonb),
    ('role-senior-analyst',
     '{"ai_autonomy.read": true, "ai_autonomy.stop": true}'::jsonb),
    ('role-analyst', '{"ai_autonomy.read": true}'::jsonb),
    ('role-viewer', '{"ai_autonomy.read": true}'::jsonb)
) AS g (role_id, grants)
WHERE r.role_id = g.role_id
  AND NOT r.permissions ?& ARRAY(SELECT jsonb_object_keys(g.grants));
