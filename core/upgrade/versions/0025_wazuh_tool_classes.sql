-- The Wazuh tool server's names (#3701), classed as 0017 classed every other
-- in-repo server's reads. A row a person already set wins.
INSERT INTO public.tool_effect_classes (tool_name, effect_class, classified_by)
SELECT tool_name, effect_class, 'system'
FROM (VALUES
    ('wazuh_wazuh_agent_log_collection', 'read_unverified'),
    ('wazuh_wazuh_list_agents', 'read_unverified'),
    ('wazuh_wazuh_list_rules', 'read_unverified'),
    ('wazuh_wazuh_test_event', 'read_unverified')
) AS seed (tool_name, effect_class)
ON CONFLICT (tool_name) DO NOTHING;
