// The chip for a data_source; its badge comes from useSourceBadge.
import { useSourceBadge } from './useSourceBadge'
import { Icon, type IconName } from './icons'
import VendorLogo, { vendorMark } from './VendorLogo'

interface SourceChipProps {
  source?: string | null
}

export default function SourceChip({ source }: SourceChipProps) {
  const { label, color, icon } = useSourceBadge()(source)
  return (
    <span
      className="source-chip"
      style={{
        color,
        background: `${color}1f`, // ~12% alpha
        borderColor: `${color}40`,
      }}
    >
      {vendorMark(`${label} ${source ?? ''}`) ? <VendorLogo name={`${label} ${source ?? ''}`} size={12} /> : <Icon name={icon as IconName} size={12} />}
      <span>{label}</span>
    </span>
  )
}
