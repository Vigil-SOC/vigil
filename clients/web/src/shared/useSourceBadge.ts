// A connector's manifest badge is the source of truth for its chip, so no
// vendor branding is hardcoded host-side; falls back to the static map.
import { sourceBadge, type SourceBadge } from '../config/sourceBadges'
import { useExtensions } from '../extensions/ExtensionProvider'

const NEUTRAL_COLOR = '#8a90a6'
const NEUTRAL_ICON = 'link'

/** Resolves a data_source to its chip badge: a loaded extension's manifest badge, else the static map. */
export function useSourceBadge(): (source?: string | null) => SourceBadge {
  const { extensions } = useExtensions()
  return (source) => {
    const key = (source || '').toLowerCase().trim()
    // data_source joins to a manifest id; a loaded extension's badge wins.
    const ext = key ? extensions.find((e) => e.manifest.id.toLowerCase() === key) : undefined
    const badge = ext?.manifest.badge
    return badge
      ? {
          label: badge.label ?? ext!.manifest.name ?? (source || '—'),
          color: badge.color ?? NEUTRAL_COLOR,
          icon: badge.icon ?? NEUTRAL_ICON,
        }
      : sourceBadge(source)
  }
}
