// A connector's manifest badge is the source of truth for its chip, so no
// vendor branding is hardcoded host-side; falls back to the static map. The
// logo resolves manifest badge, then the integration catalog's, then the icon.
import { CATALOG_TO_SOURCE, sourceBadge, type SourceBadge } from '../config/sourceBadges'
import { getAllIntegrations } from '../config/integrations'
import { useExtensions } from '../extensions/ExtensionProvider'

const NEUTRAL_COLOR = '#8a90a6'
const NEUTRAL_ICON = 'link'

// data_source -> catalog id: the reverse of CATALOG_TO_SOURCE
const SOURCE_TO_CATALOG = Object.fromEntries(Object.entries(CATALOG_TO_SOURCE).map(([id, src]) => [src, id]))

/** The catalog entry's logo; a data_source with no mapping is tried as the catalog id itself (loglm). */
function catalogLogo(key: string): string | undefined {
  const id = SOURCE_TO_CATALOG[key] ?? key
  return getAllIntegrations().find((i) => i.id === id)?.logo
}

/** Resolves a data_source to its chip badge: a loaded extension's manifest badge, else the static map. */
export function useSourceBadge(): (source?: string | null) => SourceBadge {
  const { extensions } = useExtensions()
  return (source) => {
    const key = (source || '').toLowerCase().trim()
    // data_source joins to a manifest id; a loaded extension's badge wins.
    const ext = key ? extensions.find((e) => e.manifest.id.toLowerCase() === key) : undefined
    const badge = ext?.manifest.badge
    const logo = key ? catalogLogo(key) : undefined
    return badge
      ? {
          label: badge.label ?? ext!.manifest.name ?? (source || '—'),
          color: badge.color ?? NEUTRAL_COLOR,
          icon: badge.icon ?? NEUTRAL_ICON,
          logo: badge.logo ?? logo,
        }
      : { ...sourceBadge(source), logo }
  }
}
