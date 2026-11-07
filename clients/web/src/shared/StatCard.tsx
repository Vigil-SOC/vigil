import { useId, type ReactNode } from 'react'
import './kit.css'

/**
 * A status card: label row (optional tone dot, label, ⓘ at the right in normal flow),
 * a big value and an optional sub-line. With `onClick` the card is a toggle button whose
 * `pressed` state is for assistive tech only: the filter it sets shows that it is on.
 */
export function StatCard({
  label,
  info,
  dot,
  tone,
  value,
  sub,
  onClick,
  pressed,
}: {
  label: string
  info?: ReactNode
  dot?: string // a CSS colour
  tone?: string // a CSS colour for the value
  value: ReactNode
  sub?: ReactNode
  onClick?: () => void
  pressed?: boolean
}) {
  const id = useId()
  return (
    <div className={`stat-card${onClick ? ' clickable' : ''}`} role={onClick ? undefined : 'group'} aria-label={onClick ? undefined : label}>
      {/* stretched under the content, so the ⓘ (above it) stays its own button */}
      {onClick && (
        <button type="button" className="stat-card-hit" aria-pressed={pressed} aria-labelledby={`${id}-l ${id}-v`} onClick={onClick} />
      )}
      <div className="stat-card-head">
        {dot && <span className="stat-card-dot" style={{ background: dot }} aria-hidden="true" />}
        <span id={`${id}-l`} className="stat-card-label">{label}</span>
        {info}
      </div>
      <div id={`${id}-v`} className="stat-card-val" style={tone ? { color: tone } : undefined}>{value}</div>
      {sub && <div className="stat-card-sub">{sub}</div>}
    </div>
  )
}

/** The row of status cards: four across, two by two at the reflow tier (kit.css). */
export function StatCards({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="stat-cards" role="group" aria-label={label}>
      {children}
    </div>
  )
}
