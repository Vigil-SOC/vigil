-- Lookups and the action-status read are reads; release, lift and enable are
-- acts, classed like the isolate, quarantine and disable they undo (#5058).
-- A row a person already set wins.
INSERT INTO public.tool_effect_classes (tool_name, effect_class, classified_by)
SELECT tool_name, effect_class, 'system'
FROM (VALUES
    ('microsoft-defender_mde_find_machine', 'read_unverified'),
    ('microsoft-defender_mde_get_machine_action', 'read_unverified'),
    ('microsoft-defender_mde_release', 'irreversible'),
    ('carbon-black_cb_lift_quarantine', 'irreversible'),
    ('azure-ad_aad_enable_user', 'irreversible')
) AS seed (tool_name, effect_class)
ON CONFLICT (tool_name) DO NOTHING;
