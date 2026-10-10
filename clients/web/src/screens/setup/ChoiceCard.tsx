import type { ReactNode } from 'react'

/** A selectable card: optional icon and chip, a title, a body line and an optional footer line. */
export default function ChoiceCard({
  title,
  body,
  footer,
  icon,
  chip,
  chipTone = 'ac',
  badge,
  selected,
  onSelect,
}: {
  title: string
  body?: ReactNode
  footer?: ReactNode
  icon?: ReactNode
  chip?: string
  chipTone?: 'ac' | 'vio'
  /** shown at the right of the title line */
  badge?: ReactNode
  selected: boolean
  onSelect: () => void
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      className={`su-choice${selected ? ' on' : ''}`}
      onClick={onSelect}
    >
      {(icon || chip) && (
        <span className="su-choice-top">
          {icon && <span className="su-choice-icon">{icon}</span>}
          {chip && <span className={`su-chip ${chipTone}`}>{chip}</span>}
        </span>
      )}
      {badge ? (
        <span className="su-choice-head">
          <span className="su-choice-t">{title}</span>
          {badge}
        </span>
      ) : (
        <span className="su-choice-t">{title}</span>
      )}
      {body && <span className="su-choice-s">{body}</span>}
      {footer && <span className="su-choice-s">{footer}</span>}
    </button>
  )
}
