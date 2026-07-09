/* ============================================================
   SourceChip — a small colored chip for a finding's data_source,
   using the shared source-badge map (config/sourceBadges) with a
   neutral fallback for unmapped sources. Styled with the source's
   accent color at low alpha so it reads as native in the console.
   ============================================================ */
import { sourceBadge } from '../../config/sourceBadges'
import { Icon, type IconName } from './icons'

interface SourceChipProps {
  source?: string | null
}

export default function SourceChip({ source }: SourceChipProps) {
  const { label, color, icon } = sourceBadge(source)
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
