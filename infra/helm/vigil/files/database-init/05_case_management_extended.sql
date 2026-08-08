-- Enhanced Case Management System - Initial Data
-- This script populates default SLA policies, case templates, and reference data

-- =============================================================================
-- Default SLA Policies
-- =============================================================================

-- Critical Priority SLA (1h response / 4h resolution)
INSERT INTO sla_policies (
    policy_id, name, description, priority_level,
    response_time_hours, resolution_time_hours,
    business_hours_only, notification_thresholds,
    is_active, is_default
) VALUES (
    'sla-critical-default',
    'Critical Priority SLA',
    'Standard SLA for critical priority cases requiring immediate attention',
    'critical',
    1.0, -- 1 hour response
    4.0, -- 4 hours resolution
    false, -- 24/7 coverage
    ARRAY[75, 90, 100],
    true,
    true
) ON CONFLICT (policy_id) DO NOTHING;

-- High Priority SLA (2h response / 8h resolution)
INSERT INTO sla_policies (
    policy_id, name, description, priority_level,
    response_time_hours, resolution_time_hours,
    business_hours_only, notification_thresholds,
    is_active, is_default
) VALUES (
    'sla-high-default',
    'High Priority SLA',
    'Standard SLA for high priority cases',
    'high',
    2.0, -- 2 hours response
    8.0, -- 8 hours resolution
    false, -- 24/7 coverage
    ARRAY[75, 90, 100],
    true,
    true
) ON CONFLICT (policy_id) DO NOTHING;

-- Medium Priority SLA (4h response / 24h resolution)
INSERT INTO sla_policies (
    policy_id, name, description, priority_level,
    response_time_hours, resolution_time_hours,
    business_hours_only, notification_thresholds,
    is_active, is_default
) VALUES (
    'sla-medium-default',
    'Medium Priority SLA',
    'Standard SLA for medium priority cases',
    'medium',
    4.0, -- 4 hours response
    24.0, -- 24 hours resolution
    true, -- Business hours only
    ARRAY[75, 90, 100],
    true,
    true
) ON CONFLICT (policy_id) DO NOTHING;

-- Low Priority SLA (8h response / 72h resolution)
INSERT INTO sla_policies (
    policy_id, name, description, priority_level,
    response_time_hours, resolution_time_hours,
    business_hours_only, notification_thresholds,
    is_active, is_default
) VALUES (
    'sla-low-default',
    'Low Priority SLA',
    'Standard SLA for low priority cases',
    'low',
    8.0, -- 8 hours response
    72.0, -- 72 hours (3 days) resolution
    true, -- Business hours only
    ARRAY[75, 90, 100],
    true,
    true
) ON CONFLICT (policy_id) DO NOTHING;

-- =============================================================================
-- Case Templates
-- =============================================================================

-- Malware Investigation Template
INSERT INTO case_templates (
    template_id, name, description, template_type,
    default_priority, default_status, default_sla_policy_id,
    task_templates, playbook_steps, applicable_mitre_techniques,
    tags, is_active
) VALUES (
    'template-malware-001',
    'Malware Investigation',
    'Standard template for investigating malware incidents',
    'malware',
    'high',
    'open',
    'sla-high-default',
    '[
        {"title": "Initial Triage", "description": "Assess malware type and scope", "priority": "high", "order": 1},
        {"title": "Isolate Affected Systems", "description": "Contain the malware spread", "priority": "critical", "order": 2},
        {"title": "Collect Evidence", "description": "Gather malware samples, logs, and artifacts", "priority": "high", "order": 3},
        {"title": "Analyze Malware", "description": "Perform static and dynamic analysis", "priority": "medium", "order": 4},
        {"title": "Extract IOCs", "description": "Identify indicators of compromise", "priority": "medium", "order": 5},
        {"title": "Threat Hunting", "description": "Hunt for additional compromised systems", "priority": "high", "order": 6},
        {"title": "Remediation", "description": "Remove malware and restore systems", "priority": "high", "order": 7},
        {"title": "Post-Incident Review", "description": "Document lessons learned", "priority": "low", "order": 8}
    ]'::jsonb,
    '[
        {"step": 1, "name": "Identify Malware Type", "actions": ["Review AV alerts", "Analyze file properties"]},
        {"step": 2, "name": "Containment", "actions": ["Network isolation", "Disable user accounts"]},
        {"step": 3, "name": "Eradication", "actions": ["Remove malware", "Patch vulnerabilities"]},
        {"step": 4, "name": "Recovery", "actions": ["Restore from backup", "Monitor for reinfection"]}
    ]'::jsonb,
    ARRAY['T1059', 'T1055', 'T1486', 'T1566'],
    ARRAY['malware', 'incident-response'],
    true
) ON CONFLICT (template_id) DO NOTHING;

-- Phishing Investigation Template
INSERT INTO case_templates (
    template_id, name, description, template_type,
    default_priority, default_status, default_sla_policy_id,
    task_templates, playbook_steps, applicable_mitre_techniques,
    tags, is_active
) VALUES (
    'template-phishing-001',
    'Phishing Investigation',
    'Standard template for investigating phishing attacks',
    'phishing',
    'medium',
    'open',
    'sla-medium-default',
    '[
        {"title": "Verify Phishing Report", "description": "Confirm the email is malicious", "priority": "high", "order": 1},
        {"title": "Identify Affected Users", "description": "Find who received the phishing email", "priority": "high", "order": 2},
        {"title": "Analyze Email", "description": "Extract headers, links, and attachments", "priority": "medium", "order": 3},
        {"title": "Block IOCs", "description": "Block malicious URLs, domains, and IPs", "priority": "high", "order": 4},
        {"title": "Search for Compromised Accounts", "description": "Check for successful credential theft", "priority": "high", "order": 5},
        {"title": "User Education", "description": "Notify and educate affected users", "priority": "medium", "order": 6},
        {"title": "Update Email Filters", "description": "Add rules to prevent similar attacks", "priority": "low", "order": 7}
    ]'::jsonb,
    '[
        {"step": 1, "name": "Triage", "actions": ["Review email headers", "Check sender reputation"]},
        {"step": 2, "name": "Containment", "actions": ["Delete emails from inboxes", "Block sender domain"]},
        {"step": 3, "name": "Investigation", "actions": ["Analyze attachments/links", "Check for credential use"]},
        {"step": 4, "name": "Communication", "actions": ["Alert users", "Security awareness reminder"]}
    ]'::jsonb,
    ARRAY['T1566.001', 'T1566.002', 'T1204'],
    ARRAY['phishing', 'email-security'],
    true
) ON CONFLICT (template_id) DO NOTHING;

-- Data Exfiltration Template
INSERT INTO case_templates (
    template_id, name, description, template_type,
    default_priority, default_status, default_sla_policy_id,
    task_templates, playbook_steps, applicable_mitre_techniques,
    tags, is_active
) VALUES (
    'template-data-exfiltration-001',
    'Data Exfiltration Investigation',
    'Template for investigating potential data exfiltration',
    'data_exfiltration',
    'critical',
    'open',
    'sla-critical-default',
    '[
        {"title": "Identify Data Scope", "description": "Determine what data was accessed/exfiltrated", "priority": "critical", "order": 1},
        {"title": "Identify Threat Actor", "description": "Determine who performed the exfiltration", "priority": "high", "order": 2},
        {"title": "Block Exfiltration Channels", "description": "Prevent further data loss", "priority": "critical", "order": 3},
        {"title": "Preserve Evidence", "description": "Collect logs, network captures, and artifacts", "priority": "high", "order": 4},
        {"title": "Assess Impact", "description": "Evaluate business and legal impact", "priority": "high", "order": 5},
        {"title": "Legal/Compliance Notification", "description": "Notify legal team and regulators if required", "priority": "critical", "order": 6},
        {"title": "Containment", "description": "Revoke access, rotate credentials", "priority": "high", "order": 7},
        {"title": "Forensic Analysis", "description": "Deep dive investigation", "priority": "medium", "order": 8}
    ]'::jsonb,
    '[
        {"step": 1, "name": "Detection", "actions": ["Review DLP alerts", "Analyze network traffic"]},
        {"step": 2, "name": "Containment", "actions": ["Block C2 communications", "Isolate affected systems"]},
        {"step": 3, "name": "Investigation", "actions": ["Timeline reconstruction", "Identify attack vector"]},
        {"step": 4, "name": "Recovery", "actions": ["Restore access controls", "Implement monitoring"]}
    ]'::jsonb,
    ARRAY['T1048', 'T1041', 'T1567', 'T1020'],
    ARRAY['data-breach', 'exfiltration', 'critical'],
    true
) ON CONFLICT (template_id) DO NOTHING;

-- Insider Threat Template
INSERT INTO case_templates (
    template_id, name, description, template_type,
    default_priority, default_status, default_sla_policy_id,
    task_templates, playbook_steps, applicable_mitre_techniques,
    tags, is_active
) VALUES (
    'template-insider-threat-001',
    'Insider Threat Investigation',
    'Template for investigating insider threat activities',
    'insider_threat',
    'high',
    'open',
    'sla-high-default',
    '[
        {"title": "Initial Assessment", "description": "Review alert and user activity", "priority": "high", "order": 1},
        {"title": "Preserve Evidence", "description": "Collect user activity logs and data", "priority": "critical", "order": 2},
        {"title": "Coordinate with HR/Legal", "description": "Involve HR and legal team", "priority": "high", "order": 3},
        {"title": "Analyze User Behavior", "description": "Review access patterns and anomalies", "priority": "high", "order": 4},
        {"title": "Assess Risk", "description": "Determine potential damage and intent", "priority": "high", "order": 5},
        {"title": "Containment Actions", "description": "Restrict access if necessary", "priority": "medium", "order": 6},
        {"title": "Interview Preparation", "description": "Prepare evidence for user interview", "priority": "medium", "order": 7},
        {"title": "Remediation", "description": "Implement controls to prevent recurrence", "priority": "low", "order": 8}
    ]'::jsonb,
    '[
        {"step": 1, "name": "Detection", "actions": ["UEBA alert review", "Access log analysis"]},
        {"step": 2, "name": "Evidence Collection", "actions": ["Covert monitoring", "Endpoint forensics"]},
        {"step": 3, "name": "Coordination", "actions": ["HR notification", "Legal consultation"]},
        {"step": 4, "name": "Action", "actions": ["Account suspension", "Access revocation"]}
    ]'::jsonb,
    ARRAY['T1078', 'T1530', 'T1213'],
    ARRAY['insider-threat', 'ueba'],
    true
) ON CONFLICT (template_id) DO NOTHING;

-- Ransomware Template
INSERT INTO case_templates (
    template_id, name, description, template_type,
    default_priority, default_status, default_sla_policy_id,
    task_templates, playbook_steps, applicable_mitre_techniques,
    tags, is_active
) VALUES (
    'template-ransomware-001',
    'Ransomware Incident',
    'Template for responding to ransomware attacks',
    'ransomware',
    'critical',
    'open',
    'sla-critical-default',
    '[
        {"title": "Activate Incident Response Team", "description": "Assemble full IR team", "priority": "critical", "order": 1},
        {"title": "Isolate Affected Systems", "description": "Prevent ransomware spread", "priority": "critical", "order": 2},
        {"title": "Identify Ransomware Variant", "description": "Determine ransomware family", "priority": "high", "order": 3},
        {"title": "Assess Encryption Scope", "description": "Identify all encrypted systems/files", "priority": "high", "order": 4},
        {"title": "Check Backups", "description": "Verify backup integrity and coverage", "priority": "critical", "order": 5},
        {"title": "Collect Ransom Note", "description": "Preserve ransom demands and payment info", "priority": "medium", "order": 6},
        {"title": "Notify Stakeholders", "description": "Alert executive team, legal, PR", "priority": "high", "order": 7},
        {"title": "Containment", "description": "Stop the spread and preserve evidence", "priority": "critical", "order": 8},
        {"title": "Recovery Planning", "description": "Develop restoration strategy", "priority": "high", "order": 9},
        {"title": "System Restoration", "description": "Restore from backups or rebuild", "priority": "high", "order": 10}
    ]'::jsonb,
    '[
        {"step": 1, "name": "Initial Response", "actions": ["Network segmentation", "Disable RDP/SMB"]},
        {"step": 2, "name": "Assessment", "actions": ["Identify patient zero", "Map encryption scope"]},
        {"step": 3, "name": "Containment", "actions": ["Isolate systems", "Kill processes"]},
        {"step": 4, "name": "Eradication", "actions": ["Remove ransomware", "Patch vulnerabilities"]},
        {"step": 5, "name": "Recovery", "actions": ["Restore from backup", "Verify integrity"]}
    ]'::jsonb,
    ARRAY['T1486', 'T1490', 'T1489', 'T1562'],
    ARRAY['ransomware', 'critical', 'business-continuity'],
    true
) ON CONFLICT (template_id) DO NOTHING;

-- Account Compromise Template
INSERT INTO case_templates (
    template_id, name, description, template_type,
    default_priority, default_status, default_sla_policy_id,
    task_templates, playbook_steps, applicable_mitre_techniques,
    tags, is_active
) VALUES (
    'template-account-compromise-001',
    'Account Compromise',
    'Template for investigating compromised user accounts',
    'account_compromise',
    'high',
    'open',
    'sla-high-default',
    '[
        {"title": "Verify Compromise", "description": "Confirm account compromise indicators", "priority": "high", "order": 1},
        {"title": "Disable Account", "description": "Temporarily disable compromised account", "priority": "critical", "order": 2},
        {"title": "Review Account Activity", "description": "Analyze login history and actions", "priority": "high", "order": 3},
        {"title": "Identify Attack Vector", "description": "Determine how account was compromised", "priority": "high", "order": 4},
        {"title": "Check for Lateral Movement", "description": "Hunt for additional compromised accounts", "priority": "high", "order": 5},
        {"title": "Reset Credentials", "description": "Force password reset and revoke tokens", "priority": "high", "order": 6},
        {"title": "Enable MFA", "description": "Ensure MFA is enabled for account", "priority": "medium", "order": 7},
        {"title": "User Notification", "description": "Contact account owner", "priority": "medium", "order": 8}
    ]'::jsonb,
    '[
        {"step": 1, "name": "Detection", "actions": ["Impossible travel", "Anomalous access"]},
        {"step": 2, "name": "Containment", "actions": ["Disable account", "Revoke sessions"]},
        {"step": 3, "name": "Investigation", "actions": ["Review logs", "Check for data access"]},
        {"step": 4, "name": "Recovery", "actions": ["Reset password", "Enable MFA", "Monitor"]}
    ]'::jsonb,
    ARRAY['T1078', 'T1110', 'T1552'],
    ARRAY['account-compromise', 'credential-theft'],
    true
) ON CONFLICT (template_id) DO NOTHING;

-- DDoS Attack Template
INSERT INTO case_templates (
    template_id, name, description, template_type,
    default_priority, default_status, default_sla_policy_id,
    task_templates, playbook_steps, applicable_mitre_techniques,
    tags, is_active
) VALUES (
    'template-ddos-001',
    'DDoS Attack Response',
    'Template for responding to distributed denial of service attacks',
    'ddos',
    'critical',
    'open',
    'sla-critical-default',
    '[
        {"title": "Confirm DDoS Attack", "description": "Verify attack type and scale", "priority": "critical", "order": 1},
        {"title": "Activate DDoS Mitigation", "description": "Enable DDoS protection services", "priority": "critical", "order": 2},
        {"title": "Notify ISP/CDN", "description": "Contact service providers for assistance", "priority": "high", "order": 3},
        {"title": "Analyze Traffic Patterns", "description": "Identify attack vectors and sources", "priority": "high", "order": 4},
        {"title": "Implement Filtering Rules", "description": "Block malicious traffic", "priority": "high", "order": 5},
        {"title": "Monitor Service Availability", "description": "Track mitigation effectiveness", "priority": "high", "order": 6},
        {"title": "Communicate with Stakeholders", "description": "Update business on status", "priority": "medium", "order": 7},
        {"title": "Post-Attack Analysis", "description": "Review and improve defenses", "priority": "low", "order": 8}
    ]'::jsonb,
    '[
        {"step": 1, "name": "Detection", "actions": ["Monitor traffic spikes", "Service health checks"]},
        {"step": 2, "name": "Mitigation", "actions": ["Enable rate limiting", "Activate DDoS protection"]},
        {"step": 3, "name": "Traffic Analysis", "actions": ["Identify attack type", "Block sources"]},
        {"step": 4, "name": "Recovery", "actions": ["Restore services", "Review capacity"]}
    ]'::jsonb,
    ARRAY['T1498', 'T1499'],
    ARRAY['ddos', 'availability', 'network-attack'],
    true
) ON CONFLICT (template_id) DO NOTHING;

-- Web Application Attack Template
INSERT INTO case_templates (
    template_id, name, description, template_type,
    default_priority, default_status, default_sla_policy_id,
    task_templates, playbook_steps, applicable_mitre_techniques,
    tags, is_active
) VALUES (
    'template-webapp-attack-001',
    'Web Application Attack',
    'Template for investigating web application attacks (SQL injection, XSS, etc.)',
    'web_attack',
    'high',
    'open',
    'sla-high-default',
    '[
        {"title": "Identify Attack Type", "description": "Determine attack vector (SQLi, XSS, etc.)", "priority": "high", "order": 1},
        {"title": "Assess Impact", "description": "Check if attack was successful", "priority": "critical", "order": 2},
        {"title": "Block Attack Source", "description": "Block malicious IP addresses", "priority": "high", "order": 3},
        {"title": "Review WAF Logs", "description": "Analyze web application firewall logs", "priority": "high", "order": 4},
        {"title": "Check for Data Breach", "description": "Verify if data was accessed/exfiltrated", "priority": "critical", "order": 5},
        {"title": "Patch Vulnerability", "description": "Fix exploited vulnerability", "priority": "high", "order": 6},
        {"title": "Update WAF Rules", "description": "Add rules to prevent similar attacks", "priority": "medium", "order": 7},
        {"title": "Vulnerability Scan", "description": "Scan for additional vulnerabilities", "priority": "medium", "order": 8}
    ]'::jsonb,
    '[
        {"step": 1, "name": "Detection", "actions": ["WAF alerts", "Anomalous requests"]},
        {"step": 2, "name": "Analysis", "actions": ["Review attack payload", "Check database logs"]},
        {"step": 3, "name": "Containment", "actions": ["Block attacker", "Disable vulnerable endpoint"]},
        {"step": 4, "name": "Remediation", "actions": ["Patch code", "Update WAF", "Pentest"]}
    ]'::jsonb,
    ARRAY['T1190', 'T1211', 'T1505'],
    ARRAY['web-attack', 'application-security'],
    true
) ON CONFLICT (template_id) DO NOTHING;

-- =============================================================================
-- Closure Categories Reference Data
-- =============================================================================

-- Note: Closure categories are stored as part of the CaseClosureInfo model
-- Common categories:
-- - resolved: Issue was successfully resolved
-- - false_positive: Alert was not a real security issue
-- - duplicate: Duplicate of another case
-- - unable_to_resolve: Could not be resolved
-- - mitigated: Risk mitigated but not fully resolved
-- - transferred: Transferred to another team
-- - auto_closed: Automatically closed by system

-- =============================================================================
-- Success Message
-- =============================================================================

DO $$
BEGIN
    RAISE NOTICE 'Enhanced Case Management System initialized successfully';
    RAISE NOTICE 'Created 4 default SLA policies';
    RAISE NOTICE 'Created 8 case templates';
END $$;

