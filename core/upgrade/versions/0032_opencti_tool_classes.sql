-- The OpenCTI tool server's names (#3756), classed as 0025 classed Wazuh's.
-- A row a person already set wins.
INSERT INTO public.tool_effect_classes (tool_name, effect_class, classified_by)
SELECT tool_name, effect_class, 'system'
FROM (VALUES
    ('opencti_opencti_get_indicator', 'read_unverified'),
    ('opencti_opencti_get_related', 'read_unverified'),
    ('opencti_opencti_search_entities', 'read_unverified'),
    ('opencti_opencti_search_observable', 'read_unverified')
) AS seed (tool_name, effect_class)
ON CONFLICT (tool_name) DO NOTHING;
