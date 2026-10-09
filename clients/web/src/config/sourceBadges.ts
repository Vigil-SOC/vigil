// No framework deps, so any surface can render a consistent chip.
export interface SourceBadge {
  label: string
  color: string
  icon: string
}

// Keyed by the data_source string ingestion stores, not the integration catalog id.
const SOURCE_BADGES: Record<string, SourceBadge> = {
  // loglm is intentionally absent — as a UI-extension connector its chip comes
  // from its manifest badge (see SourceChip), keeping vendor branding out of
  // host code.
  splunk: { label: 'Splunk', color: '#65a637', icon: 'search' },
  crowdstrike: { label: 'CrowdStrike', color: '#e2705f', icon: 'shield' },
  microsoft_defender: { label: 'Defender', color: '#2a7de1', icon: 'shield' },
  azure_sentinel: { label: 'Sentinel', color: '#2a7de1', icon: 'shield' },
  elastic: { label: 'Elastic', color: '#f0bf1a', icon: 'bolt' },
  opensearch: { label: 'OpenSearch', color: '#005eb8', icon: 'search' },
  aws_security_hub: { label: 'Security Hub', color: '#e88b1a', icon: 'shield' },
  darktrace: { label: 'Darktrace', color: '#8a90a6', icon: 'eye' },
  cloudflare_cloudy: { label: 'Cloudflare', color: '#8a90a6', icon: 'shield' },
  webhook: { label: 'Webhook', color: '#8a90a6', icon: 'link' },
  // The daemon's known-answer self-test (#923), not a connector.
  probe: { label: 'Probe', color: '#a78bfa', icon: 'bot' },
}

// integration catalog id -> stored data_source / federation source_id, for the catalog entries that have a collector
export const CATALOG_TO_SOURCE: Record<string, string> = {
  crowdstrike: 'crowdstrike',
  splunk: 'splunk',
  'elastic-siem': 'elastic',
  'azure-sentinel': 'azure_sentinel',
  'aws-security-hub': 'aws_security_hub',
  'microsoft-defender': 'microsoft_defender',
}

const DEFAULT_BADGE: Omit<SourceBadge, 'label'> = { color: '#8a90a6', icon: 'link' }

export function sourceBadge(source?: string | null): SourceBadge {
  const key = (source || '').toLowerCase().trim()
  const mapped = SOURCE_BADGES[key]
  if (mapped) return mapped
  return { label: source || '—', ...DEFAULT_BADGE }
}
