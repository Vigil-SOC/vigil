-- The Wazuh group read (#5220), classed as 0025 classed the server's other
-- reads. A row a person already set wins.
INSERT INTO public.tool_effect_classes (tool_name, effect_class, classified_by)
VALUES ('wazuh_wazuh_list_groups', 'read_unverified', 'system')
ON CONFLICT (tool_name) DO NOTHING;
