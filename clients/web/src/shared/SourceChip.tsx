// A connector's manifest badge is the source of truth for its chip, so no
// vendor branding is hardcoded host-side; falls back to the static map.
import { sourceBadge } from '../config/sourceBadges'
import { useExtensions } from '../extensions/ExtensionProvider'
import { Icon, type IconName } from './icons'

interface SourceChipProps {
  source?: string | null
}

const NEUTRAL_COLOR = '#8a90a6'
const NEUTRAL_ICON = 'link'

/** The label, colour and icon a source resolves to. */
export function useSourceBadge(source?: string | null) {
  const { extensions } = useExtensions()
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

export default function SourceChip({ source }: SourceChipProps) {
  const { label, color, icon } = useSourceBadge(source)
  return (
    <span
      className="source-chip"
      style={{
        color,
        background: `${color}1f`, // ~12% alpha
        borderColor: `${color}40`,
      }}
    >
      <Icon name={icon as IconName} size={12} />
      <span>{label}</span>
    </span>
  )
}
