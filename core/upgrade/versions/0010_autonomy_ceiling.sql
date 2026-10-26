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
