/* ============================================================
   Static metadata for the Integrations MCP grid, ported from the
   constants in pages/Settings.tsx (SERVER_DESCRIPTIONS, MCP_CATEGORIES,
   WIP_SERVERS, HIDDEN_MCP_SERVERS). Kept here so the redesign doesn't
   import from the legacy page.
   ============================================================ */

import { getAllIntegrations } from '../../../config/integrations'
import type { IntegrationMetadata } from '../../../components/settings/IntegrationWizard'

/** Core deps hidden from the grid (always-on / merged duplicates). */
export const HIDDEN_MCP_SERVERS = new Set(['mempalace', 'splunk-selfhosted'])

/** MCP server name → integration metadata id (where they differ). */
export const SERVER_TO_INTEGRATION: Record<string, string> = {
  'aws-security': 'aws-security-hub',
  'gcp-scc': 'gcp-security',
}

/** Resolve the credential-bearing integration metadata for an MCP server. */
export function getIntegrationForServer(serverName: string): IntegrationMetadata | undefined {
  const id = SERVER_TO_INTEGRATION[serverName] || serverName
  return getAllIntegrations().find((i) => i.id === id)
}

/** Servers with no MCP replacement yet, or kept custom for now — badged "WIP". */
export const WIP_SERVERS = new Set([
  'carbon-black', 'hybrid-analysis', 'anyrun',
  'alienvault-otx', 'palo-alto',
  'slack', 'misp', 'ip-geolocation', 'url-analysis',
  'microsoft-defender', 'azure-ad', 'microsoft-teams',
])

export interface McpCategory {
  label: string
  servers: string[]
}

/** Display categories, in order. A server matches the first category whose
    `servers` list contains it; anything unmatched lands in "Other". */
export const MCP_CATEGORIES: McpCategory[] = [
  { label: 'Internal / Platform', servers: ['deeptempo-findings', 'tempo-flow', 'approval', 'attack-layer', 'security-detections'] },
  { label: 'Reference Servers', servers: ['github'] },
  { label: 'EDR / XDR', servers: ['crowdstrike', 'sentinelone', 'carbon-black', 'microsoft-defender'] },
  { label: 'SIEM / Data Lake', servers: ['splunk', 'azure-sentinel', 'gcp-secops', 'cribl-stream'] },
  { label: 'Threat Intelligence', servers: ['virustotal', 'gcp-threat-intel', 'shodan', 'alienvault-otx', 'misp'] },
  { label: 'Cloud Security', servers: ['aws-security', 'gcp-scc', 'palo-alto'] },
  { label: 'Identity & Access', servers: ['okta', 'azure-ad'] },
  { label: 'Network Security', servers: ['vstrike'] },
  { label: 'Incident Management', servers: ['jira', 'pagerduty', 'slack', 'microsoft-teams'] },
  { label: 'Sandbox / Analysis', servers: ['joe-sandbox', 'hybrid-analysis', 'anyrun', 'url-analysis', 'ip-geolocation'] },
]

export const SERVER_DESCRIPTIONS: Record<string, string> = {
  'deeptempo-findings': 'Core findings and case management. Required for the investigation workflow, case creation, and findings display.',
  'tempo-flow': 'Orchestrates multi-step agent workflows and playbook execution. Required for automated investigation chains.',
  approval: 'Human-in-the-loop approval queue for response actions (isolate host, block IP, etc.). Prevents the AI from taking destructive actions without analyst review.',
  'attack-layer': 'Maps findings to MITRE ATT&CK techniques and generates Navigator layers for coverage visualization.',
  'security-detections': 'Searches across 30,000+ detection rules (Sigma, Splunk, Elastic, KQL). Powers detection gap analysis and rule recommendations.',
  github: 'Access GitHub repos, issues, PRs, and code search. Useful for looking up detection rule history, IaC configs, or creating remediation issues.',
  crowdstrike: 'Query CrowdStrike Falcon for endpoint detections, host info, and IOC management. Requires Falcon API credentials.',
  sentinelone: 'Query SentinelOne for endpoint threats, agent status, and threat remediation via the Purple AI MCP.',
  'carbon-black': 'Query VMware Carbon Black for endpoint events, process trees, and binary analysis.',
  'microsoft-defender': 'Query Microsoft Defender for Endpoint alerts, device info, and advanced hunting. May overlap with Sentinel.',
  splunk: 'Run SPL searches against Splunk for log analysis, correlation searches, and alert triage.',
  'azure-sentinel': 'Query Microsoft Sentinel via KQL for security logs, incidents, and custom detection rules.',
  'gcp-secops': 'Query Google SecOps (Chronicle) for UDM security events, detection rules, and threat investigation.',
  'cribl-stream': 'Manage Cribl Stream data pipelines — inspect routes, check data flow, and troubleshoot ingestion.',
  virustotal: 'Look up file hashes, URLs, domains, and IPs against VirusTotal for malware and reputation data.',
  'gcp-threat-intel': 'Google Threat Intelligence (Mandiant + VirusTotal) for threat actor profiles, campaigns, and IOC enrichment.',
  shodan: 'Search Shodan for internet-exposed devices, open ports, and service banners on IPs and domains.',
  'alienvault-otx': 'Query AlienVault OTX for community-sourced threat intelligence pulses, IOCs, and threat reports.',
  misp: 'Connect to a MISP instance for threat sharing, IOC lookups, and collaborative threat intelligence.',
  'aws-security': 'AWS Security assessment covering GuardDuty, Security Hub, Inspector, and IAM Access Analyzer findings.',
  'gcp-scc': 'Google Cloud Security Command Center for cloud asset inventory, vulnerability findings, and threat detection.',
  'palo-alto': 'Query Palo Alto Networks firewalls for threat logs, traffic analysis, and IP/domain blocking.',
  okta: 'Query Okta for user authentication events, suspicious sign-ins, and identity-based threat investigation.',
  'azure-ad': 'Query Microsoft Entra ID (Azure AD) for sign-in logs, risky users, and directory lookups.',
  jira: 'Create and manage Jira issues for incident tracking, remediation tasks, and SOC workflow integration.',
  pagerduty: 'Trigger and manage PagerDuty incidents for on-call alerting and escalation during security events.',
  slack: 'Send alerts and investigation summaries to Slack channels. Enables team collaboration during incidents.',
  'microsoft-teams': 'Post alerts and case updates to Microsoft Teams channels for SOC team communication.',
  'joe-sandbox': 'Submit files and URLs to Joe Sandbox for deep malware analysis with behavioral reports.',
  'hybrid-analysis': 'Submit samples to CrowdStrike Hybrid Analysis for free automated malware analysis and IOC extraction.',
  anyrun: 'Interactive malware sandbox for real-time analysis with process monitoring and network capture.',
  'url-analysis': 'Analyze suspicious URLs for phishing indicators, redirects, and malicious content.',
  'ip-geolocation': 'Look up geographic location, ISP, and organization info for IP addresses during investigations.',
}

/** "aws-security" → "AWS Security" */
export function prettyServerName(name: string): string {
  return name
    .split('-')
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ')
}
