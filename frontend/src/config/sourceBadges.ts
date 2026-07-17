// Generic `source → {label, color, icon}` map with a neutral fallback. Plain
// data (no framework deps) so both the redesign console and the legacy MUI
// findings table can render a consistent chip. `icon` is a redesign IconName.

export interface SourceBadge {
  label: string
  color: string
  icon: string
}

const SOURCE_BADGES: Record<string, SourceBadge> = {
  // loglm is intentionally absent — as a UI-extension connector its chip comes
  // from its manifest badge (see SourceChip), keeping vendor branding out of
  // host code.
  splunk: { label: 'Splunk', color: '#65a637', icon: 'search' },
  crowdstrike: { label: 'CrowdStrike', color: '#e2705f', icon: 'shield' },
  'microsoft-defender': { label: 'Defender', color: '#2a7de1', icon: 'shield' },
  'azure-sentinel': { label: 'Sentinel', color: '#2a7de1', icon: 'shield' },
  elastic: { label: 'Elastic', color: '#f0bf1a', icon: 'bolt' },
  'elastic-siem': { label: 'Elastic', color: '#f0bf1a', icon: 'bolt' },
  'aws-security-hub': { label: 'Security Hub', color: '#e88b1a', icon: 'shield' },
  webhook: { label: 'Webhook', color: '#8a90a6', icon: 'link' },
}

// Falls back to the raw source string as the label so nothing is hidden.
const DEFAULT_BADGE: Omit<SourceBadge, 'label'> = { color: '#8a90a6', icon: 'link' }

export function sourceBadge(source?: string | null): SourceBadge {
  const key = (source || '').toLowerCase().trim()
  const mapped = SOURCE_BADGES[key]
  if (mapped) return mapped
  return { label: source || '—', ...DEFAULT_BADGE }
}
