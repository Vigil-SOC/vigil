/* ============================================================
   Data-source badge metadata — a generic `source → {label, color, icon}`
   map with a default fallback for unmapped sources. Framework-agnostic
   (plain data) so both the redesign SOC console and the legacy MUI
   findings table can render a consistent source chip without importing
   each other. `icon` is a redesign IconName string; the legacy table
   ignores it and uses label + color only.
   ============================================================ */

export interface SourceBadge {
  label: string
  /** hex color (works for both inline styles and MUI alpha()) */
  color: string
  /** redesign IconName; consumers that don't render icons ignore it */
  icon: string
}

/** Keyed by lower-cased data_source. Add an entry to give a source its own
 *  color + icon; anything unmapped falls back to a neutral chip. */
const SOURCE_BADGES: Record<string, SourceBadge> = {
  loglm: { label: 'LogLM', color: '#7d74f3', icon: 'brain' },
  splunk: { label: 'Splunk', color: '#65a637', icon: 'search' },
  crowdstrike: { label: 'CrowdStrike', color: '#e2705f', icon: 'shield' },
  'microsoft-defender': { label: 'Defender', color: '#2a7de1', icon: 'shield' },
  'azure-sentinel': { label: 'Sentinel', color: '#2a7de1', icon: 'shield' },
  elastic: { label: 'Elastic', color: '#f0bf1a', icon: 'bolt' },
  'elastic-siem': { label: 'Elastic', color: '#f0bf1a', icon: 'bolt' },
  'aws-security-hub': { label: 'Security Hub', color: '#e88b1a', icon: 'shield' },
  webhook: { label: 'Webhook', color: '#8a90a6', icon: 'link' },
}

/** Neutral fallback for sources we don't have branding for. Keeps the label
 *  as the raw source string so nothing is hidden. */
const DEFAULT_BADGE: Omit<SourceBadge, 'label'> = { color: '#8a90a6', icon: 'link' }

/** Resolve badge metadata for a (possibly unknown / empty) data_source. */
export function sourceBadge(source?: string | null): SourceBadge {
  const key = (source || '').toLowerCase().trim()
  const mapped = SOURCE_BADGES[key]
  if (mapped) return mapped
  return { label: source || '—', ...DEFAULT_BADGE }
}
