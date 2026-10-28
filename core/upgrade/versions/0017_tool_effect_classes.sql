-- One stored effect class per outside tool (#3304). The seed is every name the
-- in-repo integration servers (core/integrations/*/tool.py) declare, flattened
-- the way the registry flattens it ({server}_{tool}), and both spellings of
-- Atomic Red Team's execute. Each class is what the retired verb heuristic
-- said at this revision: irreversible where it matched, read_unverified
-- otherwise. Vendor servers with no tool.py have no names here and stay
-- unclassified. Vigil's own tools get no row.
--
-- Startup's create_all may already have built the table from the model.

CREATE TABLE IF NOT EXISTS public.tool_effect_classes (
    tool_name text NOT NULL,
    effect_class character varying(20) NOT NULL,
    classified_by text NOT NULL,
    classified_at timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT tool_effect_classes_pkey PRIMARY KEY (tool_name),
    CONSTRAINT tool_effect_classes_effect_class_check
        CHECK (effect_class IN ('read', 'read_unverified', 'reversible', 'irreversible'))
);

-- A row a person already set wins.
INSERT INTO public.tool_effect_classes (tool_name, effect_class, classified_by)
SELECT tool_name, effect_class, 'system'
FROM (VALUES
    ('alienvault-otx_otx_check_domain', 'read_unverified'),
    ('alienvault-otx_otx_check_hash', 'read_unverified'),
    ('alienvault-otx_otx_check_ip', 'read_unverified'),
    ('anyrun_anyrun_get_report', 'read_unverified'),
    ('anyrun_anyrun_search_hash', 'read_unverified'),
    ('atomic-red-team_atomic_red_team_execute', 'irreversible'),
    ('atomic_red_team_execute', 'irreversible'),
    ('azure-ad_aad_disable_user', 'irreversible'),
    ('azure-ad_aad_get_sign_ins', 'read_unverified'),
    ('azure-ad_aad_get_user', 'read_unverified'),
    ('cape-sandbox_cape_get_iocs', 'read_unverified'),
    ('cape-sandbox_cape_get_pcap', 'read_unverified'),
    ('cape-sandbox_cape_get_report', 'read_unverified'),
    ('cape-sandbox_cape_list_tasks', 'read_unverified'),
    ('cape-sandbox_cape_search_hash', 'read_unverified'),
    ('cape-sandbox_cape_submit_file', 'read_unverified'),
    ('cape-sandbox_cape_submit_url', 'read_unverified'),
    ('cape-sandbox_cape_task_status', 'read_unverified'),
    ('carbon-black_cb_get_alerts', 'read_unverified'),
    ('carbon-black_cb_quarantine', 'irreversible'),
    ('carbon-black_cb_search_device', 'read_unverified'),
    ('cloudflare_cf_access_revoke_session', 'irreversible'),
    ('cloudflare_cf_gateway_block_domain', 'irreversible'),
    ('cloudflare_cf_lookup_domain_threat', 'read_unverified'),
    ('cloudflare_cf_lookup_ip_threat', 'read_unverified'),
    ('cloudflare_cf_waf_block_ip', 'irreversible'),
    ('cloudflare_cf_waf_unblock_ip', 'irreversible'),
    ('elastic_elastic_get_detection_alerts', 'read_unverified'),
    ('elastic_elastic_get_indices', 'read_unverified'),
    ('elastic_elastic_search_by_ioc', 'read_unverified'),
    ('elastic_elastic_search_logs', 'read_unverified'),
    ('hybrid-analysis_ha_get_report', 'read_unverified'),
    ('hybrid-analysis_ha_search_hash', 'read_unverified'),
    ('ip-geolocation_geolocate_batch', 'read_unverified'),
    ('ip-geolocation_geolocate_ip', 'read_unverified'),
    ('microsoft-defender_mde_get_alerts', 'read_unverified'),
    ('microsoft-defender_mde_get_machine', 'read_unverified'),
    ('microsoft-defender_mde_isolate', 'irreversible'),
    ('microsoft-teams_teams_send_alert', 'read_unverified'),
    ('misp_misp_get_events', 'read_unverified'),
    ('misp_misp_search_ioc', 'read_unverified'),
    ('opensearch_opensearch_get_findings', 'read_unverified'),
    ('opensearch_opensearch_get_indices', 'read_unverified'),
    ('opensearch_opensearch_search_by_ioc', 'read_unverified'),
    ('opensearch_opensearch_search_logs', 'read_unverified'),
    ('palo-alto_pan_block_ip', 'irreversible'),
    ('palo-alto_pan_get_threats', 'read_unverified'),
    ('slack_slack_send_alert', 'read_unverified'),
    ('splunk-selfhosted_splunk_execute', 'read_unverified'),
    ('splunk-selfhosted_splunk_generate_spl', 'read_unverified'),
    ('splunk-selfhosted_splunk_nl_search', 'read_unverified'),
    ('splunk-selfhosted_splunk_search_host', 'read_unverified'),
    ('splunk-selfhosted_splunk_search_ip', 'read_unverified'),
    ('url-analysis_analyze_url', 'read_unverified'),
    ('url-analysis_extract_iocs', 'read_unverified'),
    ('vstrike_vstrike_get_asset_topology', 'read_unverified'),
    ('vstrike_vstrike_get_blast_radius', 'read_unverified'),
    ('vstrike_vstrike_get_segment_findings', 'read_unverified'),
    ('vstrike_vstrike_legend_run_list', 'read_unverified'),
    ('vstrike_vstrike_legend_run_results_get', 'read_unverified'),
    ('vstrike_vstrike_list_adjacent_assets', 'read_unverified'),
    ('vstrike_vstrike_network_graph_get', 'read_unverified'),
    ('vstrike_vstrike_node_drift_get', 'read_unverified'),
    ('vstrike_vstrike_node_search', 'read_unverified'),
    ('vstrike_vstrike_storyline_events_get', 'read_unverified'),
    ('vstrike_vstrike_storyline_list', 'read_unverified'),
    ('vstrike_vstrike_ui_legend_apply', 'read_unverified'),
    ('vstrike_vstrike_ui_rightpanel_focus', 'read_unverified')
) AS seed (tool_name, effect_class)
ON CONFLICT (tool_name) DO NOTHING;
