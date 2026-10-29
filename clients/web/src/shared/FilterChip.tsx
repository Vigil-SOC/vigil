import { Icon } from './icons'
import './kit.css'

/**
 * A pill that filters a list: the active one takes the accent border and tint.
 * `list` is the 30px size the Cases list uses, with an optional state `dot`
 * (a token colour, e.g. `var(--ac)`) and a trailing `count`. A `removable` chip
 * is an applied filter that clicking takes off; it ends in a close mark.
 */
export function FilterChip({
  label,
  active,
  onClick,
  list,
  dot,
  count,
  title,
  removable,
}: {
  label: string
  active: boolean
  onClick: () => void
  list?: boolean
  dot?: string
  count?: number | string
  title?: string
  removable?: boolean
}) {
  return (
    <button
      type="button"
      className={`filter-chip${list ? ' list' : ''}${active ? ' active' : ''}`}
      aria-pressed={active}
      title={title}
      onClick={onClick}
    >
      {dot && <span className="chip-dot" style={{ background: dot }} aria-hidden="true" />}
      {label}
      {count !== undefined && <span className="chip-count">{count}</span>}
      {removable && <Icon name="close" size={11} className="chip-x" />}
    </button>
  )
}
