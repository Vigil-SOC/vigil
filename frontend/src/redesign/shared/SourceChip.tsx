/* ============================================================
   SourceChip — a small colored chip for a finding's data_source.
   A connector's own manifest is the source of truth for its chip
   (label/color/icon), so no vendor branding is hardcoded host-side;
   we fall back to the static source-badge map (config/sourceBadges)
   for non-extension sources, then a neutral default. Styled with the
   source's accent color at low alpha so it reads as native.
   ============================================================ */
import { sourceBadge } from '../../config/sourceBadges'
import { useExtensions } from '../extensions/ExtensionProvider'
import { Icon, type IconName } from './icons'

interface SourceChipProps {
  source?: string | null
}

// Neutral defaults when a manifest declares a badge but omits fields.
const NEUTRAL_COLOR = '#8a90a6'
const NEUTRAL_ICON = 'link'

export default function SourceChip({ source }: SourceChipProps) {
  const { extensions } = useExtensions()
  const key = (source || '').toLowerCase().trim()
  // A finding's data_source joins to an extension's manifest id; when that
  // extension is loaded, its manifest badge wins over the static map.
  const ext = key ? extensions.find((e) => e.manifest.id.toLowerCase() === key) : undefined
  const badge = ext?.manifest.badge
  const { label, color, icon } = badge
    ? {
        label: badge.label ?? ext!.manifest.name ?? (source || '—'),
        color: badge.color ?? NEUTRAL_COLOR,
        icon: badge.icon ?? NEUTRAL_ICON,
      }
    : sourceBadge(source)
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
